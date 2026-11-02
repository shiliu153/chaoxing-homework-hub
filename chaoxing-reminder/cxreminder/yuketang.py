"""长江雨课堂（yuketang）采集：**考试**（当前只做考试，作业暂不采集）。

鉴权：雨课堂网页版基于 Cookie，没有可用的账号密码接口，因此只支持
浏览器里的 `sessionid`（可整串 Cookie），并携带 `xtbz: ykt` 请求头。

接口（实测可用）：

    GET /api/v3/user/basic-info              登录态校验
    GET /v2/api/web/courses/list?identity=2  课程（班级）列表
    GET /v/rain_exam/get_upcoming_exam/      学生端考试列表（首页「考试」入口）
    GET /v/exam/cover?exam_id=&classroom_id= 考试封面（标题/起止时间/题数/分数/状态）

说明：

* 列表项字段为 `{id, type, title, classroom_id, classroom_name}`，
  其中 `type == "exam"` 是考试，其余（quiz 等）不是考试，直接跳过。
* 列表**不含截止时间**，因此逐条调用 `cover` 补齐 `start_time/deadline`。
* `cover.result.status`：0 未开始、1 进行中、2/3/4 已结束（已交/已批阅），
  已结束的考试不进入待办。
* 作业走章节叶子（`/api/classroom_course_chapter`）那条链路对多数专业版课程
  为空，且与考试不是同一套数据，故本版先只采集考试。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import Config
from .models import KIND_EXAM, KIND_HOMEWORK, Task
from .utils import now_in

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://changjiang.yuketang.cn"

EXAM_LIST_URL = "/v/rain_exam/get_upcoming_exam/"
EXAM_LIST_FALLBACK_URL = "/api/v3/classroom/on-lesson-upcoming-exam"
EXAM_COVER_URL = "/v/exam/cover"

# 章节叶子类型 → 待办种类（保留给后续「作业」重启时使用）
LEAF_TYPE_KIND = {
    4: KIND_HOMEWORK,
    5: KIND_EXAM,
    9: KIND_HOMEWORK,
}
_KIND_BY_NAME = (
    ("考试", KIND_EXAM), ("测验", KIND_EXAM), ("测试", KIND_EXAM),
    ("作业", KIND_HOMEWORK), ("习题", KIND_HOMEWORK), ("练习", KIND_HOMEWORK),
)

# cover.result.status → 状态文案（前端判定：4/5 已交卷、6 缺考、2 作废）
EXAM_STATUS = {
    0: "未开始",
    1: "进行中",
    2: "已作废",
    3: "待批阅",
    4: "已交卷",
    5: "已交卷",
    6: "缺考",
}


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


def exam_status_text(result: Any) -> str:
    """把 cover 里的 result.status 翻成中文；缺省视为待参加。"""
    status = _as_int(_pick(result, "status", default=None))
    if status is None:
        return "待参加"
    return EXAM_STATUS.get(status, "待参加")


def is_finished_status(status_text: str) -> bool:
    """已交卷/缺考/作废的考试不再计入待办。"""
    return any(
        word in status_text
        for word in ("已交卷", "缺考", "已作废", "已提交", "已批阅", "已结束", "已完成")
    )


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

    # ------------------------------------------------------------ 考试

    def fetch_exam_items(self) -> List[Dict[str, Any]]:
        """学生端考试列表。

        首页「考试」入口用的是 `/v/rain_exam/get_upcoming_exam/`，
        返回 `{id, type, title, classroom_id, classroom_name}` 的数组。
        另有一条账号级接口作为兜底（结构不同，字段名更杂）。
        """
        items: List[Dict[str, Any]] = []
        try:
            data = self.request("GET", EXAM_LIST_URL)
            payload = data.get("data") if isinstance(data, dict) else data
            if isinstance(payload, list):
                items = [x for x in payload if isinstance(x, dict)]
        except YuketangError as exc:
            logger.warning("[雨课堂] 考试列表接口失败：%s", exc)

        if items:
            return items

        # 兜底：账号级 upcomingExam
        try:
            data = self.request("GET", EXAM_LIST_FALLBACK_URL)
        except YuketangError:
            return []
        payload = _pick(data, "data", default={}) or {}
        for item in _pick(payload, "upcomingExam", "upcoming_exam", default=[]) or []:
            if isinstance(item, dict):
                items.append(item)
        return items

    def exam_cover(self, exam_id: str, classroom_id: str) -> Dict[str, Any]:
        """考试封面：标题、起止时间、题数、总分、作答状态。"""
        data = self.request(
            "GET", EXAM_COVER_URL,
            params={"exam_id": exam_id, "classroom_id": classroom_id},
        )
        payload = data.get("data") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else {}

    def build_exam_task(
        self,
        exam_id: Any,
        classroom_id: Any,
        title: str = "",
        course_name: str = "",
        course: Optional[YtCourse] = None,
    ) -> Optional[Task]:
        """用 exam_id 拉封面，合成一条考试待办；已交卷/缺考返回 None。"""
        exam_id = str(exam_id or "").strip()
        classroom_id = str(classroom_id or "").strip()
        if not exam_id or not classroom_id:
            return None

        cover: Dict[str, Any] = {}
        cover_error = ""
        try:
            cover = self.exam_cover(exam_id, classroom_id)
        except YuketangError as exc:
            cover_error = str(exc)[:120]
            logger.debug("[雨课堂] 考试 %s 封面获取失败：%s", exam_id, exc)

        result = _pick(cover, "result", default={}) or {}
        status_text = exam_status_text(result) if cover else "待参加"
        if cover and is_finished_status(status_text):
            return None

        final_title = title or str(_pick(cover, "title", "name", default="") or "")
        if not final_title:
            final_title = f"考试{exam_id}"

        start_at = _to_datetime(_pick(cover, "start_time", "startTime"), self.cfg.timezone)
        deadline = _to_datetime(
            _pick(cover, "deadline", "end_time", "endTime"), self.cfg.timezone
        )

        return Task(
            source="yuketang",
            kind=KIND_EXAM,
            course_id=classroom_id,
            class_id=course.cid if course else "",
            course_name=course_name or (course.name if course else "") or "雨课堂",
            task_id=exam_id,
            title=final_title,
            deadline=deadline,
            status=status_text,
            url=f"{self.base}/v2/web/exam/{classroom_id}/{exam_id}",
            extra={
                "classroom_id": classroom_id,
                "exam_id": exam_id,
                "start_at": start_at.isoformat(sep=" ", timespec="minutes") if start_at else "",
                "problem_count": _pick(cover, "problem_count", "problemCount", default=0),
                "total_score": _pick(cover, "total_score", "totalScore", default=0),
                "cover_error": cover_error,
            },
        )

    def collect(self) -> List[Task]:
        """采集全部待参加考试。

        两个来源，按 (classroom_id, exam_id) 去重：
        1. 首页考试列表 `/v/rain_exam/get_upcoming_exam/`；
        2. 课程章节树里的考试叶子（部分慕课型课程只在这里出现）。
        """
        self.login()
        courses = {c.classroom_id: c for c in self.fetch_courses()}

        tasks: List[Task] = []
        seen = set()

        def add(task: Optional[Task]) -> None:
            if not task:
                return
            ident = (task.course_id, task.task_id)
            if ident in seen:
                return
            seen.add(ident)
            tasks.append(task)

        # 1) 首页考试列表
        items = self.fetch_exam_items()
        logger.info("雨课堂考试列表：%d 条", len(items))
        for item in items:
            kind_raw = str(_pick(item, "type", "item_type", "itemType", default="")).lower()
            if kind_raw and kind_raw not in ("exam", "exam_paper"):
                continue  # quiz 等非考试
            course = courses.get(str(_pick(item, "classroom_id", "classroomId", default="")))
            try:
                add(self.build_exam_task(
                    exam_id=_pick(item, "id", "exam_id", "examId"),
                    classroom_id=_pick(item, "classroom_id", "classroomId"),
                    title=str(_pick(item, "title", "name", default="") or ""),
                    course_name=str(_pick(item, "classroom_name", "classroomName",
                                          default="") or ""),
                    course=course,
                ))
            except YuketangError as exc:
                logger.warning("[雨课堂] 考试解析失败：%s", exc)
            time.sleep(0.2)

        # 2) 章节树里的考试叶子（只取考试，作业跳过）
        for course in courses.values():
            try:
                leaves = self.fetch_course_tasks(course)
            except YuketangAuthError:
                raise
            except YuketangError as exc:
                logger.debug("[雨课堂] %s 章节读取失败：%s", course.label, exc)
                continue
            for leaf in leaves:
                # 只认显式的 leaf_type=5（考试）。按名称猜会把「阶段性测试」
                # 这类普通章节误判成考试，且拿不到 exam_id。
                if _as_int(leaf.extra.get("leaf_type")) != 5 or leaf.kind != KIND_EXAM:
                    continue
                leaf_id = leaf.extra.get("leaf_id")
                if not leaf_id:
                    continue
                try:
                    exam_id = self.resolve_leaf_exam_id(course, leaf_id)
                except YuketangError:
                    continue
                if not exam_id:
                    continue
                try:
                    add(self.build_exam_task(
                        exam_id=exam_id,
                        classroom_id=course.classroom_id,
                        title=leaf.title,
                        course=course,
                    ))
                except YuketangError:
                    continue
            time.sleep(0.2)

        logger.info("雨课堂待参加考试：%d 场", len(tasks))
        return tasks

    def resolve_leaf_exam_id(self, course: YtCourse, leaf_id: Any) -> str:
        """章节叶子 → 真实 exam_id。

        考试叶子本身只带 leaf_id，exam_id 在
        `/mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{leaf_id}/`
        的 `data.content_info.leaf_type_id` 里。
        """
        data = self.request(
            "GET",
            f"/mooc-api/v1/lms/learn/leaf_info/{course.classroom_id}/{leaf_id}/",
            headers={"classroom-id": str(course.classroom_id)},
        )
        payload = data.get("data") if isinstance(data, dict) else {}
        content = _pick(payload or {}, "content_info", "contentInfo", default={}) or {}
        exam_id = _pick(content, "leaf_type_id", "leafTypeId")
        return str(exam_id) if exam_id else ""

    # ------------------------------------------------------------ 作业（暂不采集）

    def _course_tree(self, course: YtCourse) -> Dict[str, Any]:
        """取课程章节树（作业重启时才用）。

        专业版（课堂型）走 `/api/classroom_course_chapter`，
        慕课型走 `/mooc-api/v1/lms/learn/course/chapter`。
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
        """章节叶子里的作业/考试（当前未启用，保留给作业接入）。"""
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
                _pick(leaf, "end_time", "endTime", "deadline", "expire_time"),
                self.cfg.timezone,
            )
            task = Task(
                source="yuketang",
                kind=kind,
                course_id=course.classroom_id,
                class_id=course.cid,
                course_name=course.name,
                task_id=str(leaf_id or leafinfo_id or title[:16]),
                title=title,
                deadline=deadline,
                status=self._leaf_status(leaf),
                url=f"{self.base}/v2/web/index",
                extra={
                    "leaf_id": leaf_id,
                    "leafinfo_id": leafinfo_id,
                    "leaf_type": _pick(leaf, "leaf_type", "leafType"),
                    "classroom_id": course.classroom_id,
                    "cid": course.cid,
                },
            )
            if kind == KIND_EXAM and leafinfo_id:
                task.url = (
                    f"{self.base}/v2/web/exam/{course.classroom_id}/{leafinfo_id}"
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
            "deadline": task.deadline.isoformat(sep=" ", timespec="minutes")
            if task.deadline else None,
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

        exam_id = task.extra.get("exam_id") or task.extra.get("leafinfo_id")
        if task.kind != KIND_EXAM or not exam_id:
            detail["text"] = "雨课堂当前只接入考试，作业详情暂不采集。"
            return detail

        cover = self.exam_cover(str(exam_id), str(task.extra.get("classroom_id", "")))
        result = _pick(cover, "result", default={}) or {}
        start_at = _to_datetime(_pick(cover, "start_time", "startTime"), self.cfg.timezone)
        deadline = _to_datetime(_pick(cover, "deadline", "end_time", "endTime"),
                                self.cfg.timezone)
        detail["title"] = str(_pick(cover, "title", default="") or task.title)
        detail["status"] = exam_status_text(result)
        detail["deadline"] = (
            deadline.isoformat(sep=" ", timespec="minutes") if deadline else detail["deadline"]
        )
        detail["total_score"] = _pick(cover, "total_score", "totalScore", default=0) or 0
        detail["question_count"] = _pick(cover, "problem_count", "problemCount", default=0) or 0
        if start_at:
            detail["start_at"] = start_at.isoformat(sep=" ", timespec="minutes")

        lines = [
            f"{detail['title']}",
            f"状态：{detail['status']}",
            f"开始：{start_at.strftime('%Y-%m-%d %H:%M') if start_at else '未公布'}",
            f"截止：{deadline.strftime('%Y-%m-%d %H:%M') if deadline else '未知'}",
            f"题目数：{detail['question_count']}　总分：{detail['total_score']}",
        ]
        score = _pick(result, "score", default=None)
        if score not in (None, ""):
            lines.append(f"得分：{score}")
        if cover.get("description"):
            text = re.sub(r"<[^>]+>", " ", str(cover["description"]))
            lines.append(re.sub(r"\s+", " ", text).strip()[:400])
        detail["text"] = "\n".join(lines)[:1500]
        return detail
