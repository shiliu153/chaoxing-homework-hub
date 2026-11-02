"""长江雨课堂（yuketang）采集：课程列表 / 章节叶子（作业、考试）/ 详情。

鉴权说明：雨课堂网页版完全基于 Cookie，没有可用的账号密码接口，
因此这里只支持粘贴浏览器里的 `sessionid`（可整串 Cookie），并额外携带
`xtbz: ykt` 请求头。

接口（社区整理的公开路径，已实测可达）：

    GET /v2/api/web/courses/list?identity=2      课程列表
    GET /mooc-api/v1/lms/learn/course/chapter    章节 + 叶子（作业/考试挂在这里）
    GET /mooc-api/v1/lms/learn/leaf_info/{cid}/{leaf_id}/   作业详情
    GET /v/exam/cover?exam_id=&classroom_id=     考试封面

未登录返回：{"errcode":401002,"errmsg":"Cookie has no sessionid"}
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import Config
from .models import KIND_EXAM, KIND_HOMEWORK, Course, Task
from .utils import now_in

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://changjiang.yuketang.cn"

# 叶子类型 → 待办种类（雨课堂把作业/考试都挂在章节叶子节点上）
# 4=作业、5=考试 是社区实现里最常见的映射，其余类型（视频/图文等）直接跳过。
LEAF_TYPE_KIND = {
    4: KIND_HOMEWORK,
    5: KIND_EXAM,
    9: KIND_HOMEWORK,   # 部分部署把「习题」放在 9
}
_KIND_BY_NAME = (
    ("考试", KIND_EXAM), ("测验", KIND_EXAM), ("测试", KIND_EXAM),
    ("作业", KIND_HOMEWORK), ("习题", KIND_HOMEWORK), ("练习", KIND_HOMEWORK),
)


class YuketangError(RuntimeError):
    """网络/接口异常。"""


class YuketangAuthError(YuketangError):
    """Cookie 失效或未配置。"""


def _pick(node: Any, *keys: str, default: Any = None) -> Any:
    """在 dict 里按多个候选键取值（兼容 snake_case / camelCase / PascalCase）。"""
    if not isinstance(node, dict):
        return default
    for key in keys:
        if key in node:
            value = node[key]
            if value not in (None, "", [], {}):
                return value
    return default


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any, tz: str) -> Optional[datetime]:
    """兼容秒级/毫秒级时间戳与常见日期字符串。"""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text[:19] if len(text) > 10 else text, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            dt = dt.replace(year=now_in(tz).year)
        return dt
    return None


def _walk_leaves(node: Any) -> List[Dict[str, Any]]:
    """从章节树里捞出所有叶子节点（不依赖固定的层级命名）。"""
    leaves: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        for key in ("section_leaf_list", "sectionLeafList", "leaf_list", "leafList"):
            value = node.get(key)
            if isinstance(value, list):
                leaves.extend(item for item in value if isinstance(item, dict))
        for value in node.values():
            if isinstance(value, (dict, list)):
                leaves.extend(_walk_leaves(value))
    elif isinstance(node, list):
        for item in node:
            leaves.extend(_walk_leaves(item))
    # 去重（按 leaf id）
    seen, unique = set(), []
    for leaf in leaves:
        leaf_id = _pick(leaf, "id", "leaf_id", "leafId", "leafinfo_id", "leafinfoId")
        key = str(leaf_id or id(leaf))
        if key in seen:
            continue
        seen.add(key)
        unique.append(leaf)
    return unique


def classify_leaf(leaf: Dict[str, Any]) -> Optional[str]:
    """判断叶子是作业还是考试；其它类型（视频、图文、讨论）返回 None。"""
    leaf_type = _as_int(_pick(leaf, "leaf_type", "leafType", "type"))
    if leaf_type is not None and leaf_type in LEAF_TYPE_KIND:
        return LEAF_TYPE_KIND[leaf_type]
    name = str(_pick(leaf, "name", "title", "leaf_name", default=""))
    for word, kind in _KIND_BY_NAME:
        if word in name:
            return kind
    return None


@dataclass
class YtCourse:
    classroom_id: str
    cid: str
    name: str = ""
    teacher: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or f"课程{self.classroom_id}"


class YuketangClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = (cfg.yuketang.base_url or DEFAULT_BASE).rstrip("/")
        self.timeout = cfg.request_timeout
        self.session = self._build_session()

    # ------------------------------------------------------------ 基础

    def _build_session(self):
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise YuketangError("缺少依赖 requests，请先 pip install requests") from exc

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "xtbz": "ykt",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self.base}/v2/web/index",
        })
        return session

    def _apply_cookie(self) -> None:
        raw = (self.cfg.yuketang.cookie or "").strip()
        if not raw:
            raise YuketangAuthError("未配置雨课堂 Cookie（需要 sessionid）")
        for chunk in raw.split(";"):
            if "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name, value = name.strip(), value.strip()
            if name:
                self.session.cookies.set(name, value, domain=".yuketang.cn")

    def request(self, method: str, path: str, **kwargs) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        kwargs.setdefault("timeout", self.timeout)
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, self.cfg.retry_times) + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 401:
                    raise YuketangAuthError(
                        "雨课堂 Cookie 已失效（sessionid 无效），请重新从浏览器复制"
                    )
                if resp.status_code >= 500:
                    raise YuketangError(f"服务端 {resp.status_code}")
                try:
                    data = resp.json()
                except ValueError:
                    raise YuketangError(
                        f"返回非 JSON（{resp.status_code}）：{(resp.text or '')[:120]}"
                    )
                errcode = _pick(data, "errcode", "error_code")
                if errcode in (401002, 20009, 400001) or (
                    isinstance(data, dict)
                    and str(data.get("msg", "")).lower().find("login") >= 0
                ):
                    raise YuketangAuthError(
                        f"雨课堂未登录：{_pick(data, 'errmsg', 'msg', default=errcode)}"
                    )
                return data
            except YuketangAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.cfg.retry_times:
                    break
                time.sleep(min(2 ** attempt, 8))
        raise YuketangError(f"请求失败：{url}（{last_error}）") from last_error

    def login(self) -> Dict[str, Any]:
        """用 Cookie 校验登录态，返回用户信息。"""
        self._apply_cookie()
        data = self.request("GET", "/api/v3/user/basic-info")
        info = data.get("data") if isinstance(data, dict) else None
        if not isinstance(info, dict) or not info:
            raise YuketangAuthError("雨课堂登录态校验失败：未取到用户信息")
        logger.info("雨课堂登录成功：%s", _pick(info, "name", "nickname", default="?"))
        return info

    # ------------------------------------------------------------ 课程

    def fetch_courses(self) -> List[YtCourse]:
        data = self.request("GET", "/v2/api/web/courses/list?identity=2")
        payload = data.get("data") if isinstance(data, dict) else data
        items = _pick(payload, "list", "List", "course_list", "courseList", default=[])
        if not items and isinstance(payload, list):
            items = payload

        courses: Dict[str, YtCourse] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            classroom_id = _pick(item, "classroom_id", "classroomId", "classroomID")
            if not classroom_id:
                continue
            inner = _pick(item, "course", "courseInfo", default={}) or {}
            teacher = _pick(item, "teacher", "teacher_name", "teacherName", default="")
            if isinstance(teacher, dict):
                teacher = _pick(teacher, "name", "nickname", default="")
            course = YtCourse(
                classroom_id=str(classroom_id),
                # course.id 是课程 ID；classroom_id 是班级 ID，两者不能混用
                cid=str(_pick(inner, "id", "course_id") or _pick(
                    item, "cid", "course_id", "courseId", default=classroom_id)),
                name=str(_pick(item, "name", "course_name", "courseName", default="")
                         or _pick(inner, "name", default="")),
                teacher=str(teacher or ""),
            )
            courses.setdefault(course.classroom_id, course)
        logger.info("雨课堂课程数：%d", len(courses))
        return list(courses.values())

    # ------------------------------------------------------------ 作业/考试

    def _course_tree(self, course: YtCourse) -> Dict[str, Any]:
        """取课程章节树。

        雨课堂有两套课程：
        - 专业版（课堂型，course.type=2）：/api/classroom_course_chapter?classroom_id=
        - 慕课型（学堂在线，type=1）：/mooc-api/v1/lms/learn/course/chapter
        先用课堂接口，拿不到再退回 mooc 接口。
        """
        try:
            data = self.request(
                "GET", "/api/classroom_course_chapter",
                params={"classroom_id": course.classroom_id},
            )
            tree = _pick(data, "course_chapter_tree", "courseChapterTree", default=[])
            if tree:
                return {"course_chapter": tree}
        except YuketangError as exc:
            logger.debug("[雨课堂] 课堂章节接口失败：%s", exc)

        try:
            data = self.request(
                "GET", "/mooc-api/v1/lms/learn/course/chapter",
                params={"cid": course.cid, "classroom_id": course.classroom_id},
            )
        except YuketangError as exc:
            logger.debug("[雨课堂] mooc 章节接口失败：%s", exc)
            return {}
        payload = data.get("data") if isinstance(data, dict) else {}
        return payload or {}

    def fetch_course_tasks(self, course: YtCourse) -> List[Task]:
        now = now_in(self.cfg.timezone)
        payload = self._course_tree(course)
        leaves = _walk_leaves(payload or {})

        tasks: List[Task] = []
        for leaf in leaves:
            kind = classify_leaf(leaf)
            if not kind:
                continue
            leaf_id = _pick(leaf, "id", "leaf_id", "leafId")
            leafinfo_id = _pick(leaf, "leafinfo_id", "leafinfoId", "leaf_info_id")
            title = str(_pick(leaf, "name", "title", default="")).strip()
            if not title:
                title = f"{'作业' if kind == KIND_HOMEWORK else '考试'}{leaf_id}"

            deadline = _to_datetime(
                _pick(leaf, "end_time", "endTime", "deadline", "expire_time"), self.cfg.timezone
            )
            status = self._leaf_status(leaf)
            task = Task(
                source="yuketang",
                kind=kind,
                course_id=course.classroom_id,
                class_id=course.cid,
                course_name=course.name,
                task_id=str(leaf_id or leafinfo_id or title[:16]),
                title=title,
                deadline=deadline,
                status=status,
                url=f"{self.base}/v2/web/index",
                extra={
                    "leaf_id": leaf_id,
                    "leafinfo_id": leafinfo_id,
                    "leaf_type": _pick(leaf, "leaf_type", "leafType"),
                    "classroom_id": course.classroom_id,
                    "cid": course.cid,
                    "_deadline_check": bool(deadline) and deadline < now,
                },
            )
            if kind == KIND_EXAM and leafinfo_id:
                task.url = (
                    f"{self.base}/v/exam/cover?exam_id={leafinfo_id}"
                    f"&classroom_id={course.classroom_id}"
                )
            tasks.append(task)
        return tasks

    @staticmethod
    def _leaf_status(leaf: Dict[str, Any]) -> str:
        finished = _pick(leaf, "is_finished", "isFinished", "finished", "is_complete")
        if finished in (1, True, "1", "true", "True"):
            return "已完成"
        progress = _pick(leaf, "progress", "status")
        if progress in (100, "100"):
            return "已完成"
        return "未完成"

    # ------------------------------------------------------------ 详情

    def fetch_task_detail(self, task: Task) -> Dict[str, Any]:
        now = now_in(self.cfg.timezone)
        detail: Dict[str, Any] = {
            "title": task.title,
            "course_name": task.course_name,
            "status": task.status,
            "deadline": task.deadline.isoformat(sep=" ", timespec="minutes") if task.deadline else None,
            "url": task.url,
            "groups": [],
            "questions": [],
            "attachments": [],
            "total_score": 0,
            "question_count": 0,
            "text": "",
            "cached": False,
            "fetched_at": now.isoformat(sep=" ", timespec="seconds"),
            "source": "yuketang",
        }

        if task.kind == KIND_EXAM and task.extra.get("leafinfo_id"):
            data = self.request(
                "GET",
                "/v/exam/cover",
                params={
                    "exam_id": task.extra["leafinfo_id"],
                    "classroom_id": task.extra.get("classroom_id", ""),
                },
            )
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            detail["total_score"] = _pick(payload, "total_score", "totalScore", default=0) or 0
            detail["question_count"] = _pick(payload, "problem_count", "problemCount",
                                            "question_count", default=0) or 0
            detail["text"] = self._summarize(payload)
            return detail

        leaf_id = task.extra.get("leaf_id")
        if leaf_id:
            data = self.request(
                "GET",
                f"/mooc-api/v1/lms/learn/leaf_info/{task.extra.get('cid', '')}/{leaf_id}/",
                headers={"classroom-id": str(task.extra.get("classroom_id", ""))},
            )
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            content = _pick(payload, "content_info", "contentInfo", default={}) or {}
            detail["total_score"] = _pick(
                payload, "total_score", "totalScore",
                default=_pick(content, "total_score", "totalScore", default=0)
            ) or 0
            detail["question_count"] = _pick(
                payload, "problem_count", "problemCount",
                default=_pick(content, "problem_count", default=0)
            ) or 0
            detail["text"] = self._summarize({**payload, **content})
            return detail

        detail["text"] = "该任务没有可获取的详情内容。"
        return detail

    @staticmethod
    def _summarize(payload: Dict[str, Any]) -> str:
        """把未知结构的详情压成可读文本，避免前端空白。"""
        if not isinstance(payload, dict):
            return ""
        lines: List[str] = []
        for key in ("name", "title", "description", "desc", "content", "remark"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = re.sub(r"<[^>]+>", " ", value)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    lines.append(text[:400])
        for key in ("total_score", "problem_count", "start_time", "end_time", "deadline"):
            if payload.get(key) not in (None, "", 0):
                lines.append(f"{key}: {payload[key]}")
        return "\n".join(lines)[:1500]

    # ------------------------------------------------------------ 组装

    def fetch_upcoming_exams(self, courses: List[YtCourse]) -> List[Task]:
        """账号级「即将到来的考试」（雨课堂首页提醒）。"""
        try:
            data = self.request("GET", "/api/v3/classroom/on-lesson-upcoming-exam")
        except YuketangError:
            return []
        payload = _pick(data, "data", default={}) or {}
        exams = _pick(payload, "upcomingExam", "upcoming_exam", default=[]) or []
        by_classroom = {c.classroom_id: c for c in courses}
        tasks: List[Task] = []
        for item in exams:
            if not isinstance(item, dict):
                continue
            name = str(_pick(item, "name", "title", "exam_name", default="")).strip()
            exam_id = _pick(item, "exam_id", "examId", "id")
            cr = str(_pick(item, "classroom_id", "classroomId", default="") or "")
            if not name or not exam_id:
                continue
            course = by_classroom.get(cr)
            kind = classify_leaf({"name": name}) or KIND_EXAM
            deadline = _to_datetime(
                _pick(item, "end_time", "endTime", "deadline"), self.cfg.timezone
            )
            tasks.append(Task(
                source="yuketang", kind=kind,
                course_id=cr, class_id=course.cid if course else "",
                course_name=course.name if course else "雨课堂",
                task_id=str(exam_id), title=name, deadline=deadline,
                status="待参加",
                url=f"{self.base}/v/exam/cover?exam_id={exam_id}&classroom_id={cr}",
                extra={"classroom_id": cr, "source_api": "upcoming-exam"},
            ))
        return tasks

    def collect(self) -> List[Task]:
        self.login()
        tasks: List[Task] = []
        courses = self.fetch_courses()
        tasks.extend(self.fetch_upcoming_exams(courses))
        for course in courses:
            try:
                tasks.extend(self.fetch_course_tasks(course))
            except YuketangAuthError:
                raise
            except YuketangError as exc:
                logger.warning("[雨课堂] %s 拉取失败：%s", course.label, exc)
            time.sleep(0.4)
        return tasks
