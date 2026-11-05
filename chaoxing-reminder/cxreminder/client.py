"""学习通客户端：登录、Cookie 复用、课程与待办抓取。"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional

from .config import Config, validate_account
from .crypto_aes import encrypt_password
from .models import KIND_EXAM, KIND_HOMEWORK, Course, Task
from .parsers import (
    clean_text,
    parse_course_list_html,
    parse_course_list_json,
    parse_deadline,
    parse_exam_list_html,
    parse_mooc_list_html,
    parse_task_list_html,
    parse_work_detail,
)
from .utils import ensure_dir, now_in

logger = logging.getLogger(__name__)

PASSPORT_HOST = "https://passport2.chaoxing.com"
MOOC_HOST = "https://mooc1.chaoxing.com"
COURSE_JSON_URL = "https://mooc1-1.chaoxing.com/mycourse/backclazzdata?view=json&rss=1"
COURSE_HTML_URL = "https://mooc1-1.chaoxing.com/visit/courselistdata"
# 课程「中间页」：会 302 到 mooc2-ans 的课程主页，并在页面里下发各模块的 enc 令牌
MIDDLE_URL = "https://mooc1-1.chaoxing.com/mooc-ans/visit/stucoursemiddle"
WORK_LIST_URL = f"{MOOC_HOST}/mooc2/work/list"
EXAM_LIST_URL = f"{MOOC_HOST}/exam-ans/mooc2/exam/exam-list"

# 课程页隐藏域里的令牌：enc / oldenc / workEnc / examEnc / openc
_TOKEN_RE = re.compile(r'id="(\w*[Ee]nc)" name="\1" value="([0-9a-fA-F]{16,})"')
# 未完成的作业才值得再花一次请求去详情页取截止时间
_UNFINISHED_WORDS = ("未交", "未提交", "未完成", "待完成", "待做", "未参加")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class ChaoxingError(RuntimeError):
    """网络/接口层异常。"""


class AuthError(ChaoxingError):
    """登录态失效或登录失败。"""


class ChaoxingClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = self._build_session()
        self._logged_in = False

    # ------------------------------------------------------------ 基础设施

    def _build_session(self):
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except ImportError as exc:  # pragma: no cover
            raise ChaoxingError("缺少依赖 requests，请先执行：pip install requests") from exc

        session = requests.Session()
        session.headers.update(HEADERS)
        retry = Retry(
            total=max(1, self.cfg.retry_times),
            backoff_factor=1.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", self.cfg.request_timeout)
        kwargs.setdefault("allow_redirects", True)
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, self.cfg.retry_times) + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code >= 500:
                    raise ChaoxingError(f"服务端 {resp.status_code}: {url}")
                resp.encoding = resp.encoding or "utf-8"
                return resp
            except Exception as exc:  # noqa: BLE001 - 需要兜住所有网络异常
                last_error = exc
                if attempt >= self.cfg.retry_times:
                    break
                sleep_for = min(2 ** attempt, 10) + random.uniform(0, 1.5)
                logger.warning("请求失败(%s/%s) %s：%s，%.1fs 后重试",
                               attempt, self.cfg.retry_times, url, exc, sleep_for)
                time.sleep(sleep_for)
        raise ChaoxingError(f"请求失败：{url}（{last_error}）") from last_error

    # ------------------------------------------------------------ 登录

    def login(self, force: bool = False) -> None:
        """优先复用本地 Cookie，失效则用账号密码登录。"""
        if self._logged_in and not force:
            return

        if not force and self._load_cookie_cache() and self._check_login():
            logger.info("复用本地 Cookie 登录成功")
            self._logged_in = True
            return

        if self.cfg.account.cookie:
            self._apply_cookie_string(self.cfg.account.cookie)
            if self._check_login():
                logger.info("使用配置中的 Cookie 登录成功")
                self._logged_in = True
                self._save_cookie_cache()
                return
            logger.warning("配置中的 Cookie 已失效，尝试账号密码登录")

        validate_account(self.cfg)
        self._login_by_password()
        self._logged_in = True
        self._save_cookie_cache()

    def _login_by_password(self) -> None:
        refer = "https://i.chaoxing.com"
        login_page = (
            f"{PASSPORT_HOST}/login?fid={self.cfg.account.fid or ''}"
            f"&newversion=true&refer={refer}"
        )
        try:
            self.request("GET", login_page)
        except ChaoxingError as exc:
            logger.warning("预取登录页失败（继续尝试登录）：%s", exc)

        password = (
            encrypt_password(self.cfg.account.password)
            if self.cfg.account.encrypt_password
            else self.cfg.account.password
        )
        payload = {
            "uname": self.cfg.account.username,
            "password": password,
            "t": "true",
            "forbidotherlogin": "false",
            "refer": refer,
            "leve": "1",
        }
        resp = self.request(
            "POST",
            f"{PASSPORT_HOST}/fanyalogin",
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": login_page,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            result = resp.json()
        except ValueError as exc:
            raise AuthError(
                "登录接口返回非 JSON，可能被重定向到验证码/异地登录页，"
                "请检查账号密码或改用 Cookie 方式"
            ) from exc

        ok = result.get("status") in (True, "true", 1, "1")
        if not ok:
            msg = result.get("msg") or result.get("message") or str(result)[:200]
            raise AuthError(f"登录失败：{msg}")

        # 补一次首页访问，确保拿到 _uid / _d 等 Cookie
        try:
            self.request("GET", "https://i.chaoxing.com/base")
        except ChaoxingError as exc:
            logger.debug("首页预热失败：%s", exc)
        logger.info("账号 %s 登录成功", self.cfg.account.username)

    # ------------------------------------------------------------ Cookie 持久化

    def _apply_cookie_string(self, cookie_str: str) -> None:
        for chunk in cookie_str.split(";"):
            if "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name, value = name.strip(), value.strip()
            if name:
                self.session.cookies.set(name, value, domain=".chaoxing.com")

    def _save_cookie_cache(self) -> None:
        try:
            ensure_dir(os.path.dirname(self.cfg.storage.cookie_path))
            data = {
                "cookies": [
                    {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                    for c in self.session.cookies
                ],
                "saved_at": now_in(self.cfg.timezone).isoformat(sep=" "),
            }
            with open(self.cfg.storage.cookie_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("Cookie 缓存写入失败：%s", exc)

    def _load_cookie_cache(self) -> bool:
        path = self.cfg.storage.cookie_path
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False
        cookies = data.get("cookies") or []
        if not cookies:
            return False
        for item in cookies:
            try:
                self.session.cookies.set(
                    item["name"],
                    item["value"],
                    domain=item.get("domain") or ".chaoxing.com",
                    path=item.get("path") or "/",
                )
            except (KeyError, TypeError):
                continue
        return True

    def _check_login(self) -> bool:
        """用一个轻量接口判断登录态是否有效。"""
        try:
            resp = self.request("GET", COURSE_JSON_URL, allow_redirects=False)
        except ChaoxingError:
            return False
        if resp.status_code in (301, 302, 303, 307, 308):
            return False
        body = resp.text or ""
        if '"channelList"' in body or '"course"' in body:
            return True
        # 兜底：存在 _uid 即认为已登录
        return any(c.name == "_uid" and c.value for c in self.session.cookies)

    # ------------------------------------------------------------ 抓取

    def fetch_courses(self) -> List[Course]:
        courses: List[Course] = []
        try:
            resp = self.request("GET", COURSE_JSON_URL)
            if resp.text and resp.text.strip().startswith(("{", "[")):
                courses = parse_course_list_json(resp.text)
        except ChaoxingError as exc:
            logger.warning("课程 JSON 接口失败：%s", exc)

        if not courses:
            try:
                resp = self.request(
                    "POST",
                    COURSE_HTML_URL,
                    data={
                        "courseType": "1",
                        "courseFolderId": "0",
                        "baseEducation": "0",
                        "superstarClass": "0",
                    },
                )
                courses = parse_course_list_html(resp.text or "")
            except ChaoxingError as exc:
                logger.warning("课程 HTML 页面失败：%s", exc)

        if not courses:
            raise AuthError("未能获取任何课程，登录态可能已失效或接口结构变化")

        logger.info("共获取 %d 门课程", len(courses))
        return courses

    def _build_course_params(self, course: Course) -> Dict[str, str]:
        params = {
            "courseId": course.course_id,
            "classId": course.class_id,
            "cpi": course.cpi,
            "ut": "s",
        }
        if course.enc:
            params["enc"] = course.enc
        return params

    def _open_course(self, course: Course):
        """进入课程中间页，拿到课程主页地址和各模块的 enc 令牌。

        mooc2 的作业/考试列表接口需要课程级的 workEnc / examEnc / openc，
        这些值只出现在课程主页的隐藏域里，直接调接口会返回「无权限的操作」。
        """
        resp = self.request(
            "GET",
            MIDDLE_URL,
            params={
                "courseid": course.course_id,
                "clazzid": course.class_id,
                "vc": "1",
                "cpi": course.cpi,
                "ismooc2": "1",
                "v": "2",
            },
        )
        page = resp.text or ""
        tokens = {name: value for name, value in _TOKEN_RE.findall(page)}
        return resp.url, tokens, page

    def _is_unfinished(self, status: str) -> bool:
        text = status or ""
        return any(word in text for word in _UNFINISHED_WORDS)

    def _fill_deadline(self, task: Task, referer: str) -> None:
        """未交作业进详情页取截止时间（列表页不返回）。"""
        if not task.url:
            return
        try:
            resp = self.request("GET", task.url, headers={"Referer": referer})
        except ChaoxingError as exc:
            logger.debug("作业详情页失败 %s：%s", task.url[:80], exc)
            return
        plain = clean_text(resp.text or "")
        deadline = parse_deadline(plain, now_in(self.cfg.timezone))
        if deadline:
            task.deadline = deadline

    def fetch_course_tasks(self, course: Course) -> List[Task]:
        now = now_in(self.cfg.timezone)
        tasks: List[Task] = []

        try:
            page_url, tokens, page_html = self._open_course(course)
        except ChaoxingError as exc:
            logger.warning("[%s] 课程主页打开失败：%s", course, exc)
            return tasks

        work_enc = tokens.get("workEnc") or tokens.get("enc", "")
        exam_enc = tokens.get("examEnc") or tokens.get("enc", "")
        openc = tokens.get("openc", "")

        if not work_enc:
            logger.info("[%s] 课程页未发现 enc 令牌，回退到通用解析", course)

        # ---------- 作业 ----------
        try:
            resp = self.request(
                "GET",
                WORK_LIST_URL,
                params={
                    "courseId": course.course_id,
                    "classId": course.class_id,
                    "cpi": course.cpi,
                    "ut": "s",
                    "enc": work_enc,
                },
                headers={"Referer": page_url},
            )
            body = resp.text or ""
            work_tasks = parse_mooc_list_html(body, KIND_HOMEWORK, course, now, resp.url)
            if not work_tasks:
                work_tasks = parse_task_list_html(
                    body, KIND_HOMEWORK, course, now, resp.url
                )
            tasks.extend(work_tasks)
        except ChaoxingError as exc:
            logger.warning("[%s] 作业列表失败：%s", course, exc)

        # ---------- 考试 ----------
        if exam_enc:
            try:
                resp = self.request(
                    "GET",
                    EXAM_LIST_URL,
                    params={
                        "courseid": course.course_id,
                        "clazzid": course.class_id,
                        "cpi": course.cpi,
                        "ut": "s",
                        "t": str(int(now.timestamp() * 1000)),
                        "enc": exam_enc,
                        "openc": openc,
                    },
                    headers={"Referer": page_url},
                )
                body = resp.text or ""
                if "参数传递不正确" in body:
                    logger.debug("[%s] 考试接口参数被拒", course)
                else:
                    # 考试条目没有 examId 属性，id 在 onclick="goTest(...)" 里
                    exam_tasks = parse_exam_list_html(body, course, now, resp.url)
                    if not exam_tasks:
                        exam_tasks = parse_mooc_list_html(
                            body, KIND_EXAM, course, now, resp.url
                        )
                    tasks.extend(exam_tasks)
            except ChaoxingError as exc:
                logger.warning("[%s] 考试列表失败：%s", course, exc)

        # ---------- 补齐未交作业的截止时间 ----------
        budget = 12
        for task in tasks:
            if budget <= 0:
                break
            if task.kind == KIND_HOMEWORK and task.deadline is None and self._is_unfinished(task.status):
                self._fill_deadline(task, page_url)
                budget -= 1

        if not tasks and page_html:
            logger.info("[%s] 未解析到待办（可能本课确实没有作业/考试）", course)
        return tasks

    def fetch_task_detail(self, task: Task) -> Dict[str, Any]:
        """拉取单个任务的详情（作业抓题目，考试只给信息）。"""
        if task.kind == KIND_EXAM:
            return self._exam_detail(task)
        if not task.url:
            raise ChaoxingError("该任务没有详情链接，可能来自旧缓存")
        resp = self.request("GET", task.url)
        body = resp.text or ""
        if "无权限" in body or "参数传递不正确" in body:
            raise ChaoxingError("详情页被拒绝，令牌可能已过期：请先点「立即检测」刷新后重试")
        if len(body) < 2000:
            raise ChaoxingError("详情页内容异常（长度 %d）" % len(body))

        detail = parse_work_detail(body)
        detail["title"] = task.title
        detail["course_name"] = task.course_name
        detail["status"] = task.status
        detail["deadline"] = (
            task.deadline.isoformat(sep=" ", timespec="minutes") if task.deadline else None
        )
        detail["url"] = task.url
        detail["fetched_at"] = now_in(self.cfg.timezone).isoformat(sep=" ", timespec="seconds")
        return detail

    def _exam_detail(self, task: Task) -> Dict[str, Any]:
        """考试（测验）详情。

        学习通的考试页是「开始作答」入口，没有可解析的题目正文，多数还限制
        「只允许手机 APP 作答」，所以这里不抓取，只返回状态与截止时间。
        """
        deadline = (
            task.deadline.strftime("%Y-%m-%d %H:%M") if task.deadline else "未设置"
        )
        tip = str(task.extra.get("tip") or "")
        # 课程/状态/截止已在抽屉头部展示，这里只补网页看不到的信息
        lines = []
        if tip:
            lines.append(f"作答限制：{tip}")
        lines += [
            "学习通考试（测验）需要进入作答页才能看到题目，网页端不提供题目预览。",
            "请在手机学习通里打开该课程 → 考试 → 选择对应测试；截止时间以列表为准。",
        ]
        note = tip
        return {
            "title": task.title,
            "course_name": task.course_name,
            "status": task.status,
            "deadline": task.deadline.isoformat(sep=" ", timespec="minutes")
            if task.deadline else None,
            "url": task.url,
            "groups": [],
            "questions": [],
            "attachments": [],
            "total_score": 0.0,
            "question_count": 0,
            "kind": KIND_EXAM,
            "note": note,
            "text": "\n".join(lines),
            "fetched_at": now_in(self.cfg.timezone).isoformat(sep=" ", timespec="seconds"),
        }

    def collect(self) -> List[Task]:
        self.login()
        courses = self.fetch_courses()
        self.last_course_count = len(courses)
        all_tasks: List[Task] = []
        for course in courses:
            all_tasks.extend(self.fetch_course_tasks(course))
            time.sleep(random.uniform(0.3, 1.2))  # 轻微限速，避免触发风控
        return all_tasks

    def reset(self) -> None:
        """清空登录态与 Cookie，下一轮强制重新登录。"""
        self._logged_in = False
        self.session.cookies.clear()
        try:
            if os.path.isfile(self.cfg.storage.cookie_path):
                os.remove(self.cfg.storage.cookie_path)
        except OSError:
            pass

    def dump(self, out_dir: str) -> List[str]:
        """把原始响应落盘，便于排查解析问题。"""
        ensure_dir(out_dir)
        files: List[str] = []
        stamp = now_in(self.cfg.timezone).strftime("%Y%m%d-%H%M%S")
        self.login()
        resp = self.request("GET", COURSE_JSON_URL)
        path = os.path.join(out_dir, f"courses-{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(resp.text or "")
        files.append(path)

        for course in self.fetch_courses():
            params = self._build_course_params(course)
            for kind, url in ((KIND_HOMEWORK, WORK_LIST_URL), (KIND_EXAM, EXAM_LIST_URL)):
                try:
                    r = self.request("GET", url, params=params)
                except ChaoxingError as exc:
                    logger.warning("dump %s %s 失败：%s", course, kind, exc)
                    continue
                path = os.path.join(out_dir, f"{kind}-{course.course_id}-{stamp}.html")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(r.text or "")
                files.append(path)
        return files
