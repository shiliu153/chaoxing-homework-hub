"""常驻引擎：包装 cxreminder.Runner，提供「手动触发 / 定时轮询 / 状态 / 历史」。

设计要点：
- 复用已有 cxreminder 包的全部采集与提醒逻辑（不重复实现一遍）；
- Runner.run_once 是阻塞的网络操作，放到后台线程，HTTP 接口始终可用；
- 同一时刻只允许一个采集任务，避免重复请求触发风控；
- 历史事件追加写 events.jsonl，重启不丢。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from cxreminder.client import ChaoxingClient
from cxreminder.config import Config, load_config
from cxreminder.models import Task
from cxreminder.runner import Runner
from cxreminder.utils import ensure_dir, now_in

logger = logging.getLogger("webapp.engine")


class Engine:
    def __init__(self, config_path: str, tz_name: str = "Asia/Shanghai",
                 run_semaphore=None):
        self.config_path = config_path
        self.tz_name = tz_name
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        # 多用户场景下限制同时采集的人数，避免整点一起打满外部接口
        self._run_semaphore = run_semaphore
        self._thread: Optional[threading.Thread] = None

        self.running = False
        self.last_run: Optional[datetime] = None
        self.last_ok: Optional[bool] = None
        self.last_error: str = ""
        self.next_run: Optional[datetime] = None
        self.last_summary: Dict[str, Any] = {}

        self.cfg: Config = load_config(config_path)
        self.data_dir = self.cfg.storage.data_dir
        ensure_dir(self.data_dir)
        self.events_path = os.path.join(self.data_dir, "events.jsonl")
        self._load_latest()

    # ------------------------------------------------------------ 基础

    def reload(self) -> None:
        """配置变更后重新加载（保持状态文件不变）。"""
        self.cfg = load_config(self.config_path)
        self.data_dir = self.cfg.storage.data_dir
        ensure_dir(self.data_dir)
        self.events_path = os.path.join(self.data_dir, "events.jsonl")
        logger.info("配置已重新加载：间隔 %ss，阈值 %s",
                    self.cfg.poll.interval_seconds, self.cfg.remind.thresholds_hours)

    # ------------------------------------------------------------ 忽略清单

    @property
    def ignored_path(self) -> str:
        return os.path.join(self.data_dir, "ignored.json")

    def read_ignored(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.isfile(self.ignored_path):
            return {}
        try:
            with open(self.ignored_path, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            keys = data.get("keys") or {}
            return keys if isinstance(keys, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_ignored(self, keys: Dict[str, Dict[str, Any]]) -> None:
        ensure_dir(self.data_dir)
        tmp = f"{self.ignored_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"keys": keys}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.ignored_path)

    def ignore_task(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        keys = self.read_ignored()
        keys[key] = {
            "title": str(payload.get("title") or "")[:120],
            "course": str(payload.get("course_name") or "")[:80],
            "kind": str(payload.get("kind") or "homework"),
            "source": str(payload.get("source") or "chaoxing")[:16],
            "deadline": payload.get("deadline"),
            "ignored_at": now_in(self.tz_name).isoformat(sep=" ", timespec="seconds"),
        }
        self._write_ignored(keys)
        logger.info("已忽略任务：%s / %s", keys[key]["course"], keys[key]["title"])
        return keys[key]

    def unignore_task(self, key: str) -> bool:
        keys = self.read_ignored()
        if key not in keys:
            return False
        keys.pop(key, None)
        self._write_ignored(keys)
        logger.info("已恢复任务：%s", key)
        return True

    # ------------------------------------------------------------ 详情

    def _task_index(self) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        for item in self.last_summary.get("pending") or []:
            key = self._key_of(item)
            if key:
                index[key] = item
        return index

    @staticmethod
    def _key_of(item: Dict[str, Any]) -> str:
        task = Task(
            kind=item.get("kind") or "homework",
            course_id=str(item.get("course_id") or ""),
            class_id=str(item.get("class_id") or ""),
            course_name=item.get("course_name") or "",
            task_id=str(item.get("task_id") or ""),
            title=item.get("title") or "",
            source=item.get("source") or "chaoxing",
        )
        return task.key

    def task_detail(self, key: str, refresh: bool = False) -> Dict[str, Any]:
        """按需拉取作业详情，缓存 6 小时。"""
        cache_dir = ensure_dir(os.path.join(self.data_dir, "task_cache"))
        cache_path = os.path.join(cache_dir, f"{key}.json")
        if not refresh and os.path.isfile(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                fetched = datetime.fromisoformat(str(cached.get("fetched_at")))
                if (now_in(self.tz_name) - fetched).total_seconds() < 6 * 3600:
                    return {**cached, "cached": True}
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        item = self._task_index().get(key)
        if not item:
            raise KeyError("任务不在最近一次检测结果中，请先点「立即检测」")
        source = item.get("source") or "chaoxing"
        task = Task(
            source=source,
            kind=item.get("kind") or "homework",
            course_id=str(item.get("course_id") or ""),
            class_id=str(item.get("class_id") or ""),
            course_name=item.get("course_name") or "",
            task_id=str(item.get("task_id") or ""),
            title=item.get("title") or "",
            deadline=datetime.fromisoformat(item["deadline"]) if item.get("deadline") else None,
            status=item.get("status") or "",
            url=item.get("url") or "",
        )
        if source == "yuketang":
            from cxreminder.yuketang import YuketangClient

            yt = YuketangClient(self.cfg)
            yt.login()
            detail = yt.fetch_task_detail(task)
        else:
            client = ChaoxingClient(self.cfg)
            client.login()
            detail = client.fetch_task_detail(task)
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(detail, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return {**detail, "cached": False}

    def _load_latest(self) -> None:
        path = os.path.join(self.data_dir, "summaries", "latest.json")
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                self.last_summary = json.load(fh)
            started = self.last_summary.get("started_at")
            if started:
                self.last_run = datetime.fromisoformat(started)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    # ------------------------------------------------------------ 采集

    def trigger_async(self) -> bool:
        """异步触发一次采集；已有任务在跑时返回 False。"""
        with self._state_lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run_once, daemon=True, name="collect").start()
        return True

    def run_blocking(self) -> Dict[str, Any]:
        """同步执行一次（用于 CLI 自检/首次启动）。"""
        with self._state_lock:
            if self.running:
                return {"ok": False, "error": "已有采集任务在执行"}
            self.running = True
        return self._run_once()

    def _run_once(self) -> Dict[str, Any]:
        started = now_in(self.tz_name)
        result: Dict[str, Any] = {"ok": False, "error": ""}
        try:
            with self._run_lock:
                # 每次都读最新配置，设置面板改完即生效
                guard = self._run_semaphore if self._run_semaphore is not None else nullcontext()
                with guard:
                    runner = Runner(load_config(self.config_path))
                    summary = runner.run_once()
                self.last_summary = summary.to_dict()
                counts = summary.counts()
                result = {"ok": counts["errors"] == 0, "error": "; ".join(summary.errors),
                          "counts": counts}
                self._append_events(summary, started)
                with self._state_lock:
                    self.last_ok = counts["errors"] == 0
                    self.last_error = "; ".join(summary.errors)
        except Exception as exc:  # noqa: BLE001 - 单次失败不能让服务挂掉
            logger.exception("采集失败：%s", exc)
            with self._state_lock:
                self.last_ok = False
                self.last_error = f"{type(exc).__name__}: {exc}"
            result = {"ok": False, "error": self.last_error}
        finally:
            with self._state_lock:
                self.running = False
                self.last_run = started
                interval = max(30, self.cfg.poll.interval_seconds)
                self.next_run = now_in(self.tz_name) + timedelta(seconds=interval)
        return result

    def _append_events(self, summary, started: datetime) -> None:
        events = getattr(summary, "events", None) or []
        if not events:
            return
        try:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                for event in events:
                    task = event.task
                    record = {
                        "ts": started.isoformat(sep=" ", timespec="seconds"),
                        "kind": event.kind,
                        "threshold": event.threshold,
                        "task_kind": task.kind,
                        "course": task.course_name,
                        "title": task.title,
                        "deadline": task.deadline.isoformat(sep=" ", timespec="minutes")
                        if task.deadline
                        else None,
                        "status": task.status,
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("事件写入失败：%s", exc)

    def read_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.events_path):
            return []
        try:
            with open(self.events_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------ 定时

    def start_scheduler(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("定时采集线程已启动")

    def stop_scheduler(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 启动后先跑一次，让面板尽快有数据
        if self.cfg.account.cookie or (self.cfg.account.username and self.cfg.account.password):
            self.trigger_async()
        while not self._stop.wait(10):
            if self.running:
                continue
            interval = max(30, self.cfg.poll.interval_seconds)
            last = self.last_run
            if last is None:
                self.trigger_async()
                continue
            elapsed = (now_in(self.tz_name) - last).total_seconds()
            if elapsed >= interval:
                logger.info("到达轮询间隔（%.0fs），触发采集", elapsed)
                self.trigger_async()
            else:
                with self._state_lock:
                    self.next_run = last + timedelta(seconds=interval)

    # ------------------------------------------------------------ 视图

    def source_auth(self) -> Dict[str, Dict[str, str]]:
        """两端（学习通 / 雨课堂）的登录状态，供前端弹窗提醒。

        状态取值：
        - ok       已绑定，最近一次采集没有鉴权错误
        - unbound  还没绑定（学习通无账号、雨课堂无 Cookie）
        - expired  已绑定，但最近一次采集报告登录/鉴权失败（需要重新登录）
        - unknown  已绑定，但还没跑过一轮，无法判断
        """
        errors = [str(e) for e in (self.last_summary.get("errors") or [])]
        ran = self.last_run is not None

        def find_error(*keywords: str) -> str:
            for err in errors:
                if all(k in err for k in keywords):
                    return err[:180]
            return ""

        # ---- 学习通：账号密码或 Cookie 任一存在即视为已绑定
        auth_cfg = self.cfg.account
        cx_bound = bool(auth_cfg.cookie or (auth_cfg.username and auth_cfg.password))
        cx_error = find_error("登录/鉴权失败")
        if not cx_bound:
            cx = {"state": "unbound", "detail": "尚未绑定学习通账号"}
        elif cx_error:
            cx = {"state": "expired", "detail": cx_error}
        elif not ran:
            cx = {"state": "unknown", "detail": "等待首次检测"}
        else:
            cx = {"state": "ok", "detail": "登录正常"}

        # ---- 雨课堂：只能用 Cookie，且必须显式启用
        yt_cfg = self.cfg.yuketang
        yt_bound = bool(yt_cfg.enabled and yt_cfg.cookie)
        yt_error = ""
        for err in errors:
            if "雨课堂" in err and any(w in err for w in ("登录", "Cookie", "鉴权", "401")):
                yt_error = err[:180]
                break
        if not yt_bound:
            yt = {"state": "unbound", "detail": "尚未绑定雨课堂"}
        elif yt_error:
            yt = {"state": "expired", "detail": yt_error}
        elif not ran:
            yt = {"state": "unknown", "detail": "等待首次检测"}
        else:
            yt = {"state": "ok", "detail": "登录正常"}

        return {
            "chaoxing": {"label": "学习通", **cx},
            "yuketang": {"label": "雨课堂", **yt},
        }

    def overview(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or now_in(self.tz_name)
        ignored = self.read_ignored()
        tasks = [
            t for t in (self.last_summary.get("pending") or [])
            if self._key_of(t) not in ignored
        ]
        enriched = []
        for item in tasks:
            deadline = item.get("deadline")
            remaining = None
            if deadline:
                try:
                    remaining = (
                        datetime.fromisoformat(deadline) - now
                    ).total_seconds()
                except ValueError:
                    remaining = None
            enriched.append({
                **item,
                "key": self._key_of(item),
                "remaining_seconds": remaining,
            })
        enriched.sort(
            key=lambda t: (
                t["remaining_seconds"] is None,
                t["remaining_seconds"] if t["remaining_seconds"] is not None else 0,
            )
        )

        counts = {
            "total": len(enriched),
            "homework": sum(1 for t in enriched if t.get("kind") == "homework"),
            "exam": sum(1 for t in enriched if t.get("kind") == "exam"),
            "due_24h": sum(
                1 for t in enriched
                if t["remaining_seconds"] is not None and 0 <= t["remaining_seconds"] <= 86400
            ),
            "overdue": sum(
                1 for t in enriched
                if t["remaining_seconds"] is not None and t["remaining_seconds"] < 0
            ),
        }
        ignored_list = []
        for key, meta in ignored.items():
            deadline = meta.get("deadline")
            remaining = None
            if deadline:
                try:
                    remaining = (datetime.fromisoformat(str(deadline)) - now).total_seconds()
                except ValueError:
                    remaining = None
            ignored_list.append({**meta, "key": key, "remaining_seconds": remaining})
        ignored_list.sort(key=lambda x: str(x.get("deadline") or ""))

        return {
            "now": now.isoformat(sep=" ", timespec="seconds"),
            "counts": counts,
            "tasks": enriched,
            "ignored": ignored_list,
            "events": self.read_events(30),
            "run": {
                "running": self.running,
                "last_run": self.last_run.isoformat(sep=" ", timespec="seconds")
                if self.last_run else None,
                "last_ok": self.last_ok,
                "error": self.last_error,
                "next_run": self.next_run.isoformat(sep=" ", timespec="seconds")
                if self.next_run else None,
                "interval": self.cfg.poll.interval_seconds,
                "summary": self.last_summary.get("counts") or {},
            },
            "account_ready": bool(
                self.cfg.account.cookie
                or (self.cfg.account.username and self.cfg.account.password)
            ),
            "auth": self.source_auth(),
        }
