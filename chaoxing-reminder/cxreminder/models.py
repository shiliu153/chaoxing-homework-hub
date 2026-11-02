"""数据模型：课程与待办任务。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .utils import fmt_delta, human_time

KIND_HOMEWORK = "homework"
KIND_EXAM = "exam"
KIND_LABEL = {KIND_HOMEWORK: "作业", KIND_EXAM: "考试"}


@dataclass
class Course:
    """一门课（同一 course_id 可能对应多个教学班）。"""

    course_id: str
    class_id: str
    name: str = ""
    cpi: str = ""
    enc: str = ""
    teacher: str = ""

    @property
    def key(self) -> str:
        return f"{self.course_id}:{self.class_id}"

    def __str__(self) -> str:
        return self.name or f"课程{self.course_id}"


@dataclass
class Task:
    """一条待办：作业或考试。"""

    kind: str
    course_id: str
    class_id: str
    course_name: str
    task_id: str
    title: str
    source: str = "chaoxing"          # chaoxing / yuketang
    deadline: Optional[datetime] = None
    status: str = ""
    url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    @property
    def key(self) -> str:
        """稳定主键：用于跨轮次比对，避免重复提醒。

        非默认来源加前缀，保证学习通侧的历史 key 不变（不会造成重新基线）。
        """
        base = "|".join(
            [self.kind, self.course_id, self.class_id, self.task_id or self.title]
        )
        if self.source and self.source != "chaoxing":
            base = f"{self.source}|{base}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    def remaining(self, now: datetime) -> Optional[timedelta]:
        if not self.deadline:
            return None
        return self.deadline - now

    def remaining_hours(self, now: datetime) -> Optional[float]:
        delta = self.remaining(now)
        return None if delta is None else delta.total_seconds() / 3600.0

    def remaining_text(self, now: datetime) -> str:
        delta = self.remaining(now)
        if delta is None:
            return "无截止时间"
        if delta.total_seconds() < 0:
            return f"已逾期 {fmt_delta(delta)}"
        return f"剩 {fmt_delta(delta)}"

    def deadline_text(self) -> str:
        return human_time(self.deadline)

    def is_overdue(self, now: datetime) -> bool:
        return bool(self.deadline and self.deadline < now)

    def is_pending(self, remind, now: datetime) -> bool:
        """结合状态关键字判断是否仍需关注。"""
        text = (self.status or "").strip()
        for kw in remind.ignore_status_keywords:
            if kw and kw in text:
                return False
        if text and remind.pending_status_keywords:
            hit = any(kw and kw in text for kw in remind.pending_status_keywords)
            if not hit and remind.only_pending:
                return False
        if self.deadline:
            if self.deadline < now - timedelta(days=remind.expire_grace_days):
                return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "course_id": self.course_id,
            "class_id": self.class_id,
            "course_name": self.course_name,
            "task_id": self.task_id,
            "title": self.title,
            "deadline": self.deadline.isoformat(sep=" ", timespec="minutes")
            if self.deadline
            else None,
            "status": self.status,
            "url": self.url,
        }

    def one_line(self, now: datetime) -> str:
        return (
            f"[{self.kind_label}] {self.course_name} / {self.title} | "
            f"截止 {self.deadline_text()}（{self.remaining_text(now)}）"
            + (f" | 状态 {self.status}" if self.status else "")
        )
