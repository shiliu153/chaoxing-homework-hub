"""HTML / JSON 解析：课程列表、作业列表、考试列表。

设计原则：
1. 全部为纯函数（时间通过参数注入），方便离线单测；
2. 不依赖固定的 CSS class（学习通改版频繁），而是按「页面结构特征 + 锚文本 +
   时间/状态关键字」多策略提取，任何一条命中的路径都能产出结果；
3. 解析不出截止时间时仍然保留任务（deadline=None），由上层决定是否提醒。
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import KIND_EXAM, KIND_HOMEWORK, Course, Task

# ---------------------------------------------------------------- 基础正则

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\n\u3000]+")

_DATETIME_RE = re.compile(
    r"(?<!\d)(?P<y>20\d{2})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})"
    r"(?:\s*日)?"  # 注意：这里不能用 \s*日?，否则会把后面的空格吃掉导致时间丢失
    r"(?:\s*T?\s*(?P<H>\d{1,2})\s*[:：时]\s*(?P<M>\d{1,2})"
    r"(?:\s*[:：分]\s*(?P<S>\d{1,2}))?)?"
)

_MONTH_DAY_RE = re.compile(
    r"(?<![\d:/])(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*日?\s+"
    r"(?P<H>\d{1,2})\s*[:：]\s*(?P<M>\d{1,2})(?!\d)"
)

_EPOCH_MS_RE = re.compile(r"(?<!\d)(1\d{12}|1\d{9})(?!\d)")

_DEADLINE_KEYWORDS = ("截止", "截至", "结束时间", "结束", "deadline", "expire", "endtime")

_STATUS_WORDS = [
    "未交", "未提交", "未完成", "待完成", "待做", "未开始", "进行中", "待考试",
    "待参加", "考试中", "待批阅", "已批阅", "已完成", "已提交", "已交", "已结束",
    "已过期", "缺考", "未参加",
]

_SKIP_TITLES = {
    "查看", "详情", "进入", "点击查看", "展开", "收起", "更多", "去完成", "查看详情",
    "未交", "已完成", "已交", "未开始", "进行中", "已结束", "考试", "作业",
}

_ID_PATTERNS = {
    KIND_HOMEWORK: [
        r"workid\s*[=:'\"\s]+(\d{5,})",
        r"work_id\s*[=:'\"\s]+(\d{5,})",
        r"workId['\"]?\s*[:=]\s*['\"]?(\d{5,})",
        r"toDetail\w*\s*\(\s*['\"](\d{5,})",
    ],
    KIND_EXAM: [
        r"examid\s*[=:'\"\s]+(\d{5,})",
        r"exam_id\s*[=:'\"\s]+(\d{5,})",
        r"examId['\"]?\s*[:=]\s*['\"]?(\d{5,})",
        r"toExamDetail\w*\s*\(\s*['\"](\d{5,})",
    ],
}

# 只认「明确的」通用标识，绝不回退到裸数字，否则会把 courseId / classId 误当任务 ID
_GENERIC_ID_PATTERNS = [
    r"data-(?:work|exam)?id\s*=\s*['\"](\d{5,})['\"]",
    r"[?&](?:workId|examId|taskId)=(\d{5,})",
]


# ---------------------------------------------------------------- 通用工具


def clean_text(fragment: str) -> str:
    """去标签、反转义、压缩空白。"""
    text = _TAG_RE.sub(" ", fragment or "")
    text = html_lib.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _html_blocks(page: str) -> List[str]:
    """把列表页切成单条记录的块。"""
    if not page:
        return []
    for tag in ("li", "tr"):
        parts = re.split(rf"(?i)</{tag}\s*>", page)
        blocks = [p for p in parts if p and p.strip()]
        if len(blocks) >= 2:
            return blocks
    return [page]


def _first_match(patterns: Iterable[str], text: str, flags: int = re.I) -> Optional[str]:
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- 时间解析


def parse_deadline(text: str, now: datetime) -> Optional[datetime]:
    """从一段文本中挑出「最像截止时间」的那个时间点。

    策略：
      1. 优先取紧跟在 截止/截至/结束/deadline 等关键字后的时间；
      2. 否则取文本中最大的时间（作业页通常是 发布 < 截止；考试页 开始 < 结束）；
      3. 支持 2026-11-02 23:59 / 2026年11月2日 23:59 / 11-02 23:59 / 毫秒时间戳。
    """
    if not text:
        return None
    text = html_lib.unescape(text)

    candidates: List[Tuple[int, datetime]] = []
    for m in _DATETIME_RE.finditer(text):
        try:
            dt = datetime(
                int(m.group("y")),
                int(m.group("m")),
                int(m.group("d")),
                int(m.group("H") or 23),
                int(m.group("M") or 59),
                int(m.group("S") or 0),
            )
        except ValueError:
            continue
        candidates.append((m.start(), dt))

    for m in _MONTH_DAY_RE.finditer(text):
        try:
            month, day = int(m.group("m")), int(m.group("d"))
            hour, minute = int(m.group("H")), int(m.group("M"))
            dt = datetime(now.year, month, day, hour, minute)
        except ValueError:
            continue
        # 跨年：解析结果比现在还早半年以上，视为下一年
        if dt < now - timedelta(days=180):
            try:
                dt = dt.replace(year=now.year + 1)
            except ValueError:
                pass
        candidates.append((m.start(), dt))

    if not candidates:
        m = _EPOCH_MS_RE.search(text)
        if m:
            raw = m.group(1)
            ts = int(raw) / 1000.0 if len(raw) == 13 else float(raw)
            try:
                return datetime.fromtimestamp(ts)
            except (OSError, OverflowError, ValueError):
                return None
        return None

    def keyword_score(pos: int) -> int:
        window = text[max(0, pos - 16):pos].lower()
        score = 0
        for kw in _DEADLINE_KEYWORDS:
            if kw in window:
                score += 3 if kw in ("截止", "截至") else 1
        return score

    best_score = max(keyword_score(pos) for pos, _ in candidates)
    if best_score > 0:
        pool = [dt for pos, dt in candidates if keyword_score(pos) == best_score]
        return max(pool)
    return max(dt for _, dt in candidates)


def extract_status(text: str) -> str:
    """从文本里识别任务状态词。"""
    if not text:
        return ""
    plain = clean_text(text)
    earliest: Tuple[int, str] = (10 ** 9, "")
    for word in _STATUS_WORDS:
        idx = plain.find(word)
        if idx != -1 and idx < earliest[0]:
            earliest = (idx, word)
    return earliest[1]


# ---------------------------------------------------------------- 标题提取


def _candidate_titles(block: str) -> List[str]:
    titles: List[str] = []
    for m in re.finditer(r"title\s*=\s*[\"']([^\"']{2,160})[\"']", block, re.I):
        titles.append(m.group(1))
    for m in re.finditer(r"<a\b[^>]*>(.*?)</a>", block, re.S | re.I):
        titles.append(clean_text(m.group(1)))
    for m in re.finditer(
        r"<(?:span|div|p|h\d)\b[^>]*(?:title|name|work|exam)[^>]*>(.*?)</(?:span|div|p|h\d)>",
        block,
        re.S | re.I,
    ):
        titles.append(clean_text(m.group(1)))
    return titles


def pick_title(block: str, fallback: str = "") -> str:
    for raw in _candidate_titles(block):
        title = clean_text(raw)
        if not title or len(title) < 2 or len(title) > 120:
            continue
        if title in _SKIP_TITLES:
            continue
        if re.fullmatch(r"[\d\s.:：%\-/]+", title):
            continue
        if re.fullmatch(r"(截止时间|开始时间|结束时间)[:：]?.*", title):
            continue
        return title
    return fallback


# ---------------------------------------------------------------- 课程列表


def parse_course_list_json(payload: Any) -> List[Course]:
    """解析 mycourse/backclazzdata 返回的 JSON。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []

    courses: Dict[str, Course] = {}

    def add(course_id: Any, class_id: Any, name: str, cpi: Any = "", enc: Any = "") -> None:
        cid, clz = str(course_id or "").strip(), str(class_id or "").strip()
        if not cid or not clz:
            return
        course = Course(
            course_id=cid,
            class_id=clz,
            name=(name or "").strip(),
            cpi=str(cpi or "").strip(),
            enc=str(enc or "").strip(),
        )
        courses.setdefault(course.key, course)

    for item in payload.get("channelList") or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, dict):
            continue
        data = ((content.get("course") or {}).get("data") or [])
        if not data:
            continue
        first = data[0] if isinstance(data[0], dict) else {}
        add(
            first.get("id"),
            content.get("id"),
            str(first.get("name") or ""),
            content.get("cpi"),
            content.get("enc"),
        )

    # 兼容其它返回结构：递归找形如 {course:{data:[{id,name}]}, id, cpi}
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and isinstance(content.get("course"), dict):
                data = content["course"].get("data") or []
                if data and isinstance(data[0], dict):
                    add(
                        data[0].get("id"),
                        content.get("id"),
                        str(data[0].get("name") or ""),
                        content.get("cpi"),
                        content.get("enc"),
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return list(courses.values())


_COURSE_LINK_RE = re.compile(
    r"(?:courseid|courseId)=(\d+)[^\"'<>]{0,160}?(?:clazzid|classId|clazzId)=(\d+)", re.I
)
_COURSE_LINK_REV_RE = re.compile(
    r"(?:clazzid|classId|clazzId)=(\d+)[^\"'<>]{0,160}?(?:courseid|courseId)=(\d+)", re.I
)
_COURSE_ENC_RE = re.compile(r"enc=([0-9a-zA-Z]{8,})")
_COURSE_CPI_RE = re.compile(r"cpi=(\d+)")
_COURSE_NAME_RE = re.compile(r"title\s*=\s*[\"']([^\"']{2,80})[\"']")


def parse_course_list_html(page: str) -> List[Course]:
    """解析课程卡片页（courselistdata 等）的 HTML。

    直接对全页扫课程链接，而不是先按块切分——课程卡片经常套在同一层容器里，
    按块切分会漏掉同一块内的多门课。
    """
    if not page:
        return []
    courses: Dict[str, Course] = {}

    for pattern, order in ((_COURSE_LINK_RE, "cid"), (_COURSE_LINK_REV_RE, "clz")):
        for m in pattern.finditer(page):
            if order == "cid":
                course_id, class_id = m.group(1), m.group(2)
            else:
                class_id, course_id = m.group(1), m.group(2)

            window = page[max(0, m.start() - 600): m.end() + 600]
            m_enc = _COURSE_ENC_RE.search(page[m.start(): m.end() + 600])
            m_cpi = _COURSE_CPI_RE.search(page[m.start(): m.end() + 600])
            name = ""
            m_name = _COURSE_NAME_RE.search(window)
            if m_name:
                name = m_name.group(1).strip()
            if not name:
                name = pick_title(window)

            course = Course(
                course_id=course_id,
                class_id=class_id,
                name=name,
                cpi=m_cpi.group(1) if m_cpi else "",
                enc=m_enc.group(1) if m_enc else "",
            )
            courses.setdefault(course.key, course)

    return list(courses.values())


# ---------------------------------------------------------------- 任务列表


def _extract_task_id(block: str, kind: str) -> Optional[str]:
    found = _first_match(_ID_PATTERNS.get(kind, []), block)
    if found:
        return found
    return _first_match(_GENERIC_ID_PATTERNS, block)


def parse_task_list_html(
    page: str,
    kind: str,
    course: Course,
    now: datetime,
    base_url: str = "",
) -> List[Task]:
    """从作业/考试列表页 HTML 中抽取任务。"""
    if not page:
        return []

    tasks: Dict[str, Task] = {}
    for block in _html_blocks(page):
        plain = clean_text(block)
        if not plain and "<" not in block:
            continue

        task_id = _extract_task_id(block, kind)
        title = pick_title(block)
        deadline = parse_deadline(plain, now)
        status = extract_status(plain)

        # 至少要有一个可辨识特征，避免把表头/空行当任务
        if not task_id and not (title and (deadline or status)):
            continue
        if not title:
            title = f"{'作业' if kind == KIND_HOMEWORK else '考试'}{task_id or ''}"
        if not task_id:
            task_id = re.sub(r"\s+", "", title)[:24]

        task = Task(
            kind=kind,
            course_id=course.course_id,
            class_id=course.class_id,
            course_name=course.name,
            task_id=str(task_id),
            title=title,
            deadline=deadline,
            status=status,
            url=base_url,
        )
        tasks.setdefault(task.key, task)

    return list(tasks.values())


def parse_task_list_json(
    payload: Any, kind: str, course: Course, now: datetime, base_url: str = ""
) -> List[Task]:
    """部分接口返回 JSON 列表（兼容态），递归提取含标题与时间的对象。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, (dict, list)):
        return []

    title_keys = ("title", "name", "workName", "examName", "taskName")
    time_keys = ("endTime", "deadline", "end_time", "expireTime", "examEndTime")
    id_keys = ("id", "workId", "examId", "taskId")
    found: Dict[str, Task] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            title = next(
                (str(node[k]) for k in title_keys if node.get(k)), ""
            )
            if title:
                deadline = None
                for k in time_keys:
                    if node.get(k):
                        raw = str(node[k])
                        if raw.isdigit() and len(raw) >= 10:
                            ts = int(raw) / (1000 if len(raw) >= 13 else 1)
                            try:
                                deadline = datetime.fromtimestamp(ts)
                            except (OSError, ValueError):
                                deadline = None
                        else:
                            deadline = parse_deadline(raw, now)
                        if deadline:
                            break
                task_id = next(
                    (str(node[k]) for k in id_keys if node.get(k)), title[:24]
                )
                task = Task(
                    kind=kind,
                    course_id=course.course_id,
                    class_id=course.class_id,
                    course_name=course.name,
                    task_id=task_id,
                    title=clean_text(title),
                    deadline=deadline,
                    status=str(node.get("status") or node.get("state") or ""),
                    url=base_url,
                )
                found.setdefault(task.key, task)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return list(found.values())


# ---------------------------------------------------------------- 学习通 mooc2 真站结构
# 作业/考试列表页（/mooc2/work/list、/exam-ans/mooc2/exam/exam-list）的实际结构：
#   <li onclick="goTask(this);" data="...workId=57254804&answerId=..&enc=.."
#       aria-label="新建作业20261020201100 ; 待批阅">
#     <p class="overHidden2 fl">新建作业20261020201100</p>
#     <p class="status fl">待批阅</p>
#   </li>
# 列表本身不含截止时间，未交项需要再进详情页取。

_MOOC_ITEM_ATTR_RE = re.compile(r'\bdata="([^"]*?(?:workId|examId)=\d+[^"]*)"', re.I)
_ARIA_RE = re.compile(r'aria-label="([^"]{2,160})"', re.I)
_OVERHIDDEN_RE = re.compile(r'class="[^"]*overHidden\d?[^"]*"[^>]*>([^<]{1,160})<', re.I)
_STATUS_P_RE = re.compile(r'class="[^"]*status[^"]*"[^>]*>\s*([^<]{1,30})<', re.I)


def _iter_mooc_items(page: str) -> Iterable[str]:
    """按 <li> 切分 mooc2 列表页。"""
    for chunk in re.split(r"(?i)<li\b", page or "")[1:]:
        yield chunk.split("</li>")[0]


def parse_mooc_list_html(
    page: str,
    kind: str,
    course: Course,
    now: datetime,
    base_url: str = "",
) -> List[Task]:
    """解析 mooc2 作业/考试列表。"""
    tasks: Dict[str, Task] = {}
    id_key = "workId" if kind == KIND_HOMEWORK else "examId"

    for block in _iter_mooc_items(page):
        if id_key.lower() not in block.lower():
            continue
        m_attr = _MOOC_ITEM_ATTR_RE.search(block)
        detail_url = html_lib.unescape(m_attr.group(1)) if m_attr else ""
        m_id = re.search(rf"{id_key}=(\d+)", detail_url, re.I)
        if not m_id and kind == KIND_HOMEWORK:
            m_id = re.search(r"workid=(\d+)", detail_url, re.I)
        if not m_id:
            continue

        title, status = "", ""
        # 优先用列表项正文里的标题，aria-label 只在其带 "; 状态" 时采用，
        # 否则容易把「筛选」「作业列表」这类辅助标签当成标题。
        m_title = _OVERHIDDEN_RE.search(block)
        if m_title:
            title = clean_text(m_title.group(1))
        m_aria = _ARIA_RE.search(block)
        if m_aria and ";" in m_aria.group(1):
            parts = [p.strip() for p in m_aria.group(1).split(";")]
            if not title:
                title = parts[0]
            if len(parts) > 1:
                status = parts[-1]
        if not status:
            m_status = _STATUS_P_RE.search(block)
            if m_status:
                status = clean_text(m_status.group(1))
        if not title:
            title = f"{'作业' if kind == KIND_HOMEWORK else '考试'}{m_id.group(1)}"

        plain = clean_text(block)
        deadline = parse_deadline(plain, now) if ("截止" in plain or "结束" in plain) else None

        task = Task(
            kind=kind,
            course_id=course.course_id,
            class_id=course.class_id,
            course_name=course.name,
            task_id=m_id.group(1),
            title=title,
            deadline=deadline,
            status=status,
            url=detail_url,
        )
        tasks.setdefault(task.key, task)

    return list(tasks.values())


# ---------------------------------------------------------------- 作业详情页
# 结构（/mooc-ans/mooc2/work/task）：
#   <h2 class="type_tit">一. 简答题（共1题，50分）</h2>
#   <div class="... questionLi ..." typeName="简答题" id="question406044460">
#     <h3 class="mark_name workTextWrap">1.<span class="colorShallow">(简答题)</span><p>题干…</p></h3>
#     <div class="stem_answer"> …选项/输入框… </div>
#   </div>

_QUESTION_ID_RE = re.compile(r'id="question(\d+)"', re.I)
_TYPE_NAME_RE = re.compile(r'typeName="([^"]{1,20})"', re.I)
_TYPE_TIT_RE = re.compile(r'<h2[^>]*class="[^"]*type_tit[^"]*"[^>]*>(.*?)</h2>', re.S | re.I)
_STEM_RE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.S | re.I)
_OPTION_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_SCORE_RE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]")
_ATTACH_AREA_RE = re.compile(r'<div[^>]*class="[^"]*(?:workAttach|attachNew|attach)[^"]*"[^>]*>(.*?)</div>',
                             re.S | re.I)
_ATTACH_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def parse_work_detail(page: str) -> Dict[str, Any]:
    """解析作业详情页，返回题目、分值、附件与纯文本摘要。"""
    result: Dict[str, Any] = {
        "groups": [],
        "questions": [],
        "attachments": [],
        "total_score": 0.0,
        "text": "",
    }
    if not page:
        return result

    # 大题分组：一. 简答题（共1题，50分）
    group_spans = []  # [(起始位置, 标题, 题数, 该组总分, 题型名)]
    for m in _TYPE_TIT_RE.finditer(page):
        title = clean_text(m.group(1))
        if not title:
            continue
        result["groups"].append(title)
        count, score = 0, 0.0
        m_score = re.search(r"共\s*(\d+)\s*题[，,]\s*(\d+(?:\.\d+)?)\s*分", title)
        if m_score:
            count, score = int(m_score.group(1)), float(m_score.group(2))
        else:
            m_any = re.search(r"(\d+(?:\.\d+)?)\s*分", title)
            if m_any:
                score = float(m_any.group(1))
        result["total_score"] += score
        m_type = re.search(r"[.、．]\s*([^\s（(]+)", title)
        group_spans.append((m.start(), title, count, score, m_type.group(1) if m_type else ""))

    starts = [(m.start(), m.group(1)) for m in _QUESTION_ID_RE.finditer(page)]
    for idx, (pos, qid) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else min(len(page), pos + 30000)
        block = page[pos:end]

        group = None
        for item in group_spans:
            if item[0] < pos:
                group = item
        # 题型优先取所属大题标题；没有大题时再看本题 div 上的 typeName
        # （typeName 在 id="question" 之前，必须往前开窗口，否则会抓到下一题）
        qtype = group[4] if group else ""
        if not qtype:
            m_type = _TYPE_NAME_RE.search(page[max(0, pos - 400): pos + 400])
            qtype = clean_text(m_type.group(1)) if m_type else ""

        stem_html = ""
        m_stem = _STEM_RE.search(block)
        if m_stem:
            stem_html = m_stem.group(1)
        # 去掉题号与题型标签，只留题干
        stem_html = re.sub(r'<span[^>]*class="[^"]*colorShallow[^"]*"[^>]*>.*?</span>', " ",
                           stem_html, flags=re.S | re.I)
        stem = clean_text(stem_html)
        stem = re.sub(r"^\s*\d+\s*[.、]\s*", "", stem).strip()

        options: List[str] = []
        head = block.split("stem_answer", 1)[0]
        for m_opt in _OPTION_RE.finditer(head):
            text = clean_text(m_opt.group(1))
            if text and len(text) <= 300:
                options.append(text)

        score = 0.0
        m_score = _SCORE_RE.search(clean_text(block[:4000])[:600])
        if m_score:
            score = float(m_score.group(1))
        elif group and group[2] == 1 and group[3]:
            score = round(group[3], 2)  # 单题大题：直接继承大题分值

        result["questions"].append({
            "no": idx + 1,
            "id": qid,
            "type": qtype,
            "stem": stem[:2000],
            "options": options[:12],
            "score": score,
        })

    # 附件
    for m_area in _ATTACH_AREA_RE.finditer(page):
        for m_link in _ATTACH_LINK_RE.finditer(m_area.group(1)):
            href = html_lib.unescape(m_link.group(1))
            name = clean_text(m_link.group(2))
            if href.startswith(("javascript:", "#")) or not name:
                continue
            result["attachments"].append({"name": name[:120], "url": href})
    # 去重
    seen = set()
    unique = []
    for item in result["attachments"]:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    result["attachments"] = unique[:20]

    if not result["total_score"]:
        result["total_score"] = sum(q["score"] for q in result["questions"])

    lines = []
    if result["groups"]:
        lines.append(" / ".join(result["groups"]))
    for q in result["questions"]:
        prefix = f"{q['no']}. "
        if q["type"]:
            prefix += f"[{q['type']}] "
        lines.append(prefix + q["stem"])
        for opt in q["options"]:
            lines.append("    " + opt)
    result["text"] = "\n".join(lines)[:4000]
    result["question_count"] = len(result["questions"])
    return result


# ---------------------------------------------------------------- 考试列表（exam-ans）
# 结构（/exam-ans/mooc2/exam/exam-list）：
#   <div class="bottomList"><ul>
#     <li>
#       <div onclick="goTest('266525113',11040808,171558311,'2026-11-07 23:36:42.0',
#                             572383735,false,'fb6134cb...','0');">
#         <p class="overHidden2 fl">影视英语与文化-测试5-翻译 2</p>
#         <p class="status fl">待做</p>
#       </div>
#       <div class="time ...">剩余61小时21分钟</div>
#     </li>
#     <li><div onclick="viewExamAnswer('10981917','171406590')"> ... 已完成 ... </div></li>
#   </ul></div>
#
# 与作业列表不同：考试条目没有 workId/examId 属性，id 只出现在 onclick 参数里，
# 所以 parse_mooc_list_html 对它完全失效（之前 0 条考试的根因）。

_EXAM_ACTION_RE = re.compile(r"(goTest|viewExamAnswer)\s*\(([^)]*)\)", re.I)
_EXAM_ARG_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|(-?\d+)")
_EXAM_DT_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_EXAM_LEFT_RE = re.compile(
    r"剩余\s*(?:(\d+)\s*天)?\s*(\d+)\s*小时\s*(\d+)\s*分钟"
)
# 「只允许手机APP考试」「只允许手机APP或电脑考试客户端考试」这类限制提示
_EXAM_TIP_RE = re.compile(
    r'class="[^"]*span_tip[^"]*"[^>]*>(.*?)</div>', re.S | re.I
)


def _split_exam_args(raw: str) -> List[str]:
    """拆分 onclick 里的参数（单双引号字符串 + 裸数字）。"""
    args: List[str] = []
    for m in _EXAM_ARG_RE.finditer(raw or ""):
        value = m.group(1) if m.group(1) is not None else (
            m.group(2) if m.group(2) is not None else m.group(3)
        )
        args.append((value or "").strip())
    return args


def _exam_deadline(raw: str, block: str, now: datetime) -> Optional[datetime]:
    """优先取 onclick 里的完整时间；没有则用「剩余 X 天 Y 小时 Z 分钟」推算。"""
    m = _EXAM_DT_RE.search(raw or "")
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6) or 0),
                tzinfo=now.tzinfo,
            )
        except ValueError:
            pass
    m_left = _EXAM_LEFT_RE.search(clean_text(block))
    if m_left:
        days = int(m_left.group(1) or 0)
        return now + timedelta(
            days=days, hours=int(m_left.group(2)), minutes=int(m_left.group(3))
        )
    return None


def parse_exam_list_html(
    page: str,
    course: Course,
    now: datetime,
    base_url: str = "",
) -> List[Task]:
    """解析考试列表页（exam-ans）。"""
    tasks: Dict[str, Task] = {}

    for block in _iter_mooc_items(page):
        m_action = _EXAM_ACTION_RE.search(block)
        if not m_action:
            continue
        action = m_action.group(1)
        raw_args = m_action.group(2)
        args = _split_exam_args(raw_args)

        # goTest(courseId, examId, relationId, deadline, ...)
        # viewExamAnswer(examId, relationId)
        if action.lower() == "gotest":
            exam_id = args[1] if len(args) > 1 else ""
            relation_id = args[2] if len(args) > 2 else ""
        else:
            exam_id = args[0] if args else ""
            relation_id = args[1] if len(args) > 1 else ""
        if not exam_id.isdigit():
            continue

        title = ""
        m_title = _OVERHIDDEN_RE.search(block)
        if m_title:
            title = clean_text(m_title.group(1))
        if not title:
            title = f"考试{exam_id}"

        status = ""
        m_status = _STATUS_P_RE.search(block)
        if m_status:
            status = clean_text(m_status.group(1))

        tip = ""
        m_tip = _EXAM_TIP_RE.search(block)
        if m_tip:
            tip = clean_text(m_tip.group(1))

        task = Task(
            kind=KIND_EXAM,
            course_id=course.course_id,
            class_id=course.class_id,
            course_name=course.name,
            task_id=exam_id,
            title=title,
            deadline=_exam_deadline(raw_args, block, now),
            status=status,
            url=base_url or "",
            extra={
                "exam_id": exam_id,
                "relation_id": relation_id,
                "action": action,
                "tip": tip,
                # gTest 的参数里夹着 false 这类裸词，不能按位置取；enc 是 32 位十六进制
                "enc": next(
                    (a for a in args if re.fullmatch(r"[0-9a-fA-F]{24,}", a)), ""
                ),
            },
        )
        tasks.setdefault(task.key, task)

    return list(tasks.values())
