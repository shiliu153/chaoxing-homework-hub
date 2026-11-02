"""轮询主循环与提醒决策。

核心流程（每轮）：
    登录 -> 拉课程 -> 逐课拉作业/考试 -> 过滤待办 -> 与状态比对
         -> 生成事件（新任务 / 即将到期 / 已逾期） -> 推送 -> 落盘汇总
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .client import AuthError, ChaoxingClient, ChaoxingError
from .config import Config, ConfigError
from .models import Task
from .notify import Notifier
from .state import StateStore
from .utils import ensure_dir, fmt_delta, human_time, now_in

logger = logging.getLogger(__name__)

EVENT_NEW = "new"
EVENT_DUE = "due"
EVENT_OVERDUE = "overdue"


@dataclass
class Event:
    kind: str
    task: Task
    threshold: Optional[float] = None

    def headline(self, now: datetime) -> str:
        return (
            f"[{self.task.kind_label}] {self.task.course_name} / {self.task.title}"
            f"  ->  截止 {self.task.deadline_text()}（{self.task.remaining_text(now)}）"
        )


@dataclass
class Summary:
    started_at: datetime
    finished_at: datetime
    now: datetime
    all_tasks: List[Task] = field(default_factory=list)
    pending: List[Task] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    bootstrap: bool = False
    errors: List[str] = field(default_factory=list)
    notify_results: List[Any] = field(default_factory=list)
    courses: int = 0

    # ---------------------------------------------------------- 输出

    def counts(self) -> Dict[str, int]:
        return {
            "courses": self.courses,
            "all": len(self.all_tasks),
            "pending": len(self.pending),
            "new": sum(1 for e in self.events if e.kind == EVENT_NEW),
            "due": sum(1 for e in self.events if e.kind == EVENT_DUE),
            "overdue": sum(1 for e in self.events if e.kind == EVENT_OVERDUE),
            "errors": len(self.errors),
        }

    def summary_line(self) -> str:
        c = self.counts()
        return (
            f"课程 {c['courses']} / 抓取 {c['all']} 条 / 待办 {c['pending']} 条 | "
            f"新任务 {c['new']} · 即将到期 {c['due']} · 已逾期 {c['overdue']}"
            + (f" | 异常 {c['errors']}" if c["errors"] else "")
        )

    def to_console(self) -> str:
        lines = [
            "=" * 64,
            f"学习通待办巡检 @ {self.started_at:%Y-%m-%d %H:%M:%S}",
            self.summary_line(),
            "-" * 64,
        ]
        if self.bootstrap:
            lines.append("（首轮基线：仅登记不逐条播报，后续新增/临期才会提醒）")
        if self.pending:
            ordered = sorted(
                self.pending,
                key=lambda t: (t.deadline is None, t.deadline or datetime.max),
            )
            for idx, task in enumerate(ordered, 1):
                lines.append(f"{idx:>2}. {task.one_line(self.now)}")
        else:
            lines.append("暂无待办，保持关注。")
        if self.errors:
            lines.append("-" * 64)
            for err in self.errors:
                lines.append(f"[异常] {err}")
        if self.notify_results:
            lines.append("-" * 64)
            for name, ok, detail in self.notify_results:
                lines.append(f"[推送] {name}: {'成功' if ok else '失败'} - {detail}")
        lines.append("=" * 64)
        return "\n".join(lines)

    def to_markdown(self, title: Optional[str] = None) -> str:
        c = self.counts()
        lines = [f"# {title or '学习通待办提醒'}", ""]
        lines.append(f"- 巡检时间：{self.started_at:%Y-%m-%d %H:%M:%S}")
        lines.append(f"- 概览：{self.summary_line()}")
        lines.append("")

        grouped = {
            EVENT_OVERDUE: [e for e in self.events if e.kind == EVENT_OVERDUE],
            EVENT_DUE: [e for e in self.events if e.kind == EVENT_DUE],
            EVENT_NEW: [e for e in self.events if e.kind == EVENT_NEW],
        }
        headings = {
            EVENT_OVERDUE: "已逾期",
            EVENT_DUE: "即将到期",
            EVENT_NEW: "新任务",
        }
        for kind in (EVENT_OVERDUE, EVENT_DUE, EVENT_NEW):
            items = grouped[kind]
            if not items:
                continue
            lines.append(f"### {headings[kind]}（{len(items)} 条）")
            for event in items:
                task = event.task
                extra = ""
                if event.threshold is not None:
                    extra = f"（触发阈值 {event.threshold:g}h）"
                lines.append(
                    f"- 【{task.kind_label}】{task.course_name} / {task.title}{extra}\n"
                    f"  截止 {task.deadline_text()}，{task.remaining_text(self.now)}"
                    + (f"，状态 {task.status}" if task.status else "")
                )
            lines.append("")

        lines.append(f"### 全部待办（{c['pending']} 条）")
        if self.pending:
            ordered = sorted(
                self.pending,
                key=lambda t: (t.deadline is None, t.deadline or datetime.max),
            )
            for idx, task in enumerate(ordered, 1):
                lines.append(
                    f"{idx}. 【{task.kind_label}】{task.course_name} / {task.title} —— "
                    f"{task.deadline_text()}（{task.remaining_text(self.now)}）"
                )
        else:
            lines.append("暂无")
        if self.errors:
            lines.append("")
            lines.append("### 异常")
            for err in self.errors:
                lines.append(f"- {err}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(sep=" ", timespec="seconds"),
            "finished_at": self.finished_at.isoformat(sep=" ", timespec="seconds"),
            "counts": self.counts(),
            "bootstrap": self.bootstrap,
            "pending": [t.to_dict() for t in self.pending],
            "events": [
                {
                    "kind": e.kind,
                    "threshold": e.threshold,
                    "task": e.task.to_dict(),
                }
                for e in self.events
            ],
            "errors": self.errors,
        }


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = cfg.timezone
        self.state = StateStore(cfg.storage.state_path, tz_name=cfg.timezone)
        self.notifier = Notifier(
            cfg.channels, dry_run=cfg.dry_run, timeout=cfg.request_timeout
        )
        self._client: Optional[ChaoxingClient] = None

    # -------------------------------------------------------------- 客户端

    @property
    def client(self) -> ChaoxingClient:
        if self._client is None:
            self._client = ChaoxingClient(self.cfg)
        return self._client

    # -------------------------------------------------------------- 单轮

    def run_once(self) -> Summary:
        started = now_in(self.tz)
        errors: List[str] = []
        all_tasks: List[Task] = []
        courses = 0

        try:
            all_tasks = self.client.collect()
            courses = getattr(self.client, "last_course_count", 0) or len(
                {(t.course_id, t.class_id) for t in all_tasks}
            )
        except AuthError as exc:
            errors.append(f"登录/鉴权失败：{exc}")
            self.client.reset()
        except ConfigError as exc:
            # 配置类问题不打印堆栈，只作为本轮异常记录
            logger.error("配置不完整：%s", exc)
            errors.append(f"配置错误：{exc}")
        except ChaoxingError as exc:
            errors.append(f"网络或接口异常：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单轮失败不应中断守护进程
            logger.exception("采集阶段发生未预期异常")
            errors.append(f"未预期异常：{exc}")

        if self.cfg.yuketang.enabled and self.cfg.yuketang.cookie:
            yt_tasks, yt_courses, yt_errors = self._collect_yuketang()
            all_tasks.extend(yt_tasks)
            courses += yt_courses
            errors.extend(yt_errors)

        now = now_in(self.tz)
        # summary 保留全量待办（含用户忽略的），提醒只针对未忽略的
        pending = [t for t in all_tasks if t.is_pending(self.cfg.remind, now)]
        pending.sort(key=lambda t: (t.deadline is None, t.deadline or datetime.max))

        bootstrap = self.state.is_empty()
        ignored = self.ignored_keys()
        active = [t for t in pending if t.key not in ignored]
        events = self._diff(active, now, bootstrap)

        summary = Summary(
            started_at=started,
            finished_at=now_in(self.tz),
            now=now,
            all_tasks=all_tasks,
            pending=pending,
            events=events,
            bootstrap=bootstrap,
            errors=errors,
            courses=courses,
        )

        should_notify = bool(events) or (bootstrap and bool(pending))
        daily_due = self._daily_summary_due(now)
        if should_notify or daily_due:
            title = self._build_title(summary, daily_due)
            body = self._build_body(summary, daily_due)
            summary.notify_results = self.notifier.send(title, body)
            if daily_due:
                self.state.set_meta("last_daily_summary", now.strftime("%Y-%m-%d"))

        if events:
            logger.info("本轮事件：%s", summary.summary_line())
        elif errors and not all_tasks:
            logger.warning("本轮未采集到任何数据：%s", errors[0])
        else:
            logger.info("本轮无新事件；%s", summary.summary_line())

        self.state.prune()
        self.state.save()
        self._write_summary(summary)
        return summary

    def _collect_yuketang(self):
        """采集长江雨课堂的作业与考试（失败不影响学习通侧结果）。"""
        from .yuketang import YuketangAuthError, YuketangClient, YuketangError

        tasks: List[Task] = []
        errors: List[str] = []
        try:
            client = YuketangClient(self.cfg)
            client.login()
            courses = client.fetch_courses()
        except YuketangAuthError as exc:
            return tasks, 0, [f"雨课堂登录失败：{exc}"]
        except Exception as exc:  # noqa: BLE001
            return tasks, 0, [f"雨课堂异常：{exc}"]

        for course in courses:
            try:
                tasks.extend(client.fetch_course_tasks(course))
            except YuketangError as exc:
                errors.append(f"雨课堂《{course.label}》：{exc}")
            time.sleep(0.3)
        logger.info("雨课堂：%d 门课程，%d 条待办", len(courses), len(tasks))
        return tasks, len(courses), errors

    def ignored_keys(self) -> set:
        """用户手动忽略的任务不参与提醒（与网页共用 data/ignored.json）。"""
        path = os.path.join(self.cfg.storage.data_dir, "ignored.json")
        if not os.path.isfile(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return set((data or {}).get("keys", {}).keys())
        except (OSError, json.JSONDecodeError):
            return set()

    # -------------------------------------------------------------- 事件判定

    def _diff(self, pending: List[Task], now: datetime, bootstrap: bool) -> List[Event]:
        events: List[Event] = []
        thresholds = self.cfg.remind.thresholds_hours
        remind = self.cfg.remind

        for task in pending:
            entry = self.state.get(task.key)
            first_time = entry is None
            payload = {
                "title": task.title,
                "kind": task.kind,
                "course": task.course_name,
                "deadline": task.deadline.isoformat(sep=" ", timespec="minutes")
                if task.deadline
                else None,
                "status": task.status,
            }
            entry = self.state.mark_seen(task.key, payload)

            # 首轮基线：只封存「此刻已经触发」的阈值，避免历史任务一次性刷屏；
            # 尚未进入任一阈值的任务，后续跨阈值时仍会正常提醒。
            if first_time and bootstrap:
                remaining = task.remaining_hours(now)
                seeded = [th for th in thresholds if remaining is not None and remaining <= th]
                entry["new_notified"] = True
                entry["thresholds_notified"] = sorted(set(seeded))
                if remaining is not None and remaining <= 0:
                    entry["overdue_notified"] = True
                continue

            if first_time:
                if remind.notify_new:
                    events.append(Event(EVENT_NEW, task))
                entry["new_notified"] = True
                # 新任务当下已经进入的阈值一并封存：新任务提醒本身已包含紧迫度，
                # 无需下一轮再补一条“即将到期”，只保留更紧的阈值。
                remaining = task.remaining_hours(now)
                seeded = [th for th in thresholds if remaining is not None and remaining <= th]
                entry["thresholds_notified"] = sorted(set(seeded))
                if remaining is not None and remaining <= 0:
                    entry["overdue_notified"] = True
                continue

            remaining = task.remaining_hours(now)
            if remaining is None:
                continue

            if remaining <= 0:
                if remind.notify_overdue and not entry.get("overdue_notified"):
                    events.append(Event(EVENT_OVERDUE, task))
                    entry["overdue_notified"] = True
                continue

            done = set(entry.get("thresholds_notified") or [])
            crossed = [th for th in thresholds if remaining <= th and th not in done]
            if crossed:
                tightest = min(crossed)
                entry["thresholds_notified"] = sorted(done | set(crossed))
                events.append(Event(EVENT_DUE, task, threshold=tightest))

        return events

    # -------------------------------------------------------------- 消息组装

    def _build_title(self, summary: Summary, daily_due: bool) -> str:
        c = summary.counts()
        parts = []
        if c["overdue"]:
            parts.append(f"{c['overdue']} 条已逾期")
        if c["due"]:
            parts.append(f"{c['due']} 条即将到期")
        if c["new"]:
            parts.append(f"{c['new']} 条新任务")
        if not parts:
            parts.append(f"当前待办 {c['pending']} 条")
        suffix = "（每日汇总）" if daily_due and not summary.events else ""
        return "学习通提醒：" + "、".join(parts) + suffix

    def _build_body(self, summary: Summary, daily_due: bool) -> str:
        md = summary.to_markdown()
        limit = self.cfg.remind.max_items_per_message
        if limit > 0:
            lines = md.splitlines()
            if len(lines) > limit * 4 + 20:
                md = "\n".join(lines[: limit * 4 + 20]) + "\n\n（内容过长已截断，完整清单见本地汇总文件）"
        if summary.errors:
            md += "\n\n> 本轮存在异常，可能未覆盖全部课程，详见日志。"
        return md

    # -------------------------------------------------------------- 每日汇总

    def _daily_summary_due(self, now: datetime) -> bool:
        target = self.cfg.poll.daily_summary_at
        if not target:
            return False
        try:
            hour, minute = (int(x) for x in target.split(":"))
        except ValueError:
            return False
        if now.hour * 60 + now.minute < hour * 60 + minute:
            return False
        return self.state.get_meta("last_daily_summary") != now.strftime("%Y-%m-%d")

    # -------------------------------------------------------------- 落盘

    def _write_summary(self, summary: Summary) -> None:
        out_dir = ensure_dir(self.cfg.storage.summary_path)
        try:
            stamp = summary.started_at.strftime("%Y%m%d-%H%M%S")
            md_path = os.path.join(out_dir, f"summary-{stamp}.md")
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(summary.to_markdown())
            with open(os.path.join(out_dir, "latest.md"), "w", encoding="utf-8") as fh:
                fh.write(summary.to_markdown())
            with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as fh:
                json.dump(summary.to_dict(), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("汇总文件写入失败：%s", exc)

    # -------------------------------------------------------------- 守护循环

    def run_forever(self, max_cycles: int = 0) -> None:
        interval = max(30, self.cfg.poll.interval_seconds)
        jitter = max(0, self.cfg.poll.jitter_seconds)
        cycle = 0
        logger.info(
            "开始轮询：间隔 %ss（抖动 ±%ss），阈值 %s 小时，通道 %s",
            interval,
            jitter,
            self.cfg.remind.thresholds_hours,
            [c.get("type") for c in self.cfg.channels],
        )
        while True:
            cycle += 1
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - 守护进程必须自愈
                logger.exception("第 %d 轮巡检失败：%s", cycle, exc)
                try:
                    self.client.reset()
                except Exception:  # noqa: BLE001
                    pass

            if max_cycles and cycle >= max_cycles:
                logger.info("已达到最大轮询次数 %d，退出", max_cycles)
                break

            sleep_for = interval + random.uniform(-jitter, jitter)
            sleep_for = max(30.0, sleep_for)
            next_at = now_in(self.tz) + timedelta(seconds=sleep_for)
            logger.info("第 %d 轮结束，下一轮约 %s（%.0fs 后）",
                        cycle, next_at.strftime("%m-%d %H:%M:%S"), sleep_for)
            time.sleep(sleep_for)
