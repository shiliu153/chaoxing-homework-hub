"""状态持久化：记录已见任务、已发提醒，实现跨轮次去重。"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .utils import ensure_dir, now_in

logger = logging.getLogger(__name__)

STATE_VERSION = 1


class StateStore:
    """JSON 状态文件，写入使用「临时文件 + 原子替换」，防止进程被杀导致损坏。"""

    def __init__(self, path: str, tz_name: str = "Asia/Shanghai"):
        self.path = path
        self.tz_name = tz_name
        self.data: Dict[str, Any] = {"version": STATE_VERSION, "tasks": {}, "meta": {}}
        self._load()

    # -------------------------------------------------------------- IO

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            logger.info("状态文件不存在，将以首轮结果作为基线：%s", self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
                self.data = data
                self.data.setdefault("meta", {})
                self.data["version"] = STATE_VERSION
            else:
                logger.warning("状态文件结构异常，已重置：%s", self.path)
        except (OSError, json.JSONDecodeError) as exc:
            backup = f"{self.path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            logger.warning("状态文件损坏（%s），已备份到 %s 并重置", exc, backup)

    def save(self) -> None:
        ensure_dir(os.path.dirname(os.path.abspath(self.path)))
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.error("状态文件写入失败：%s", exc)

    # -------------------------------------------------------------- 查询

    @property
    def tasks(self) -> Dict[str, Dict[str, Any]]:
        return self.data["tasks"]

    def is_empty(self) -> bool:
        return not self.data.get("tasks")

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self.data["tasks"].get(key)

    def forget(self, key: str) -> bool:
        """删除单条记录，让任务在下一轮被当作「新任务」处理。"""
        return self.data["tasks"].pop(key, None) is not None

    def mark_seen(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        entry = self.data["tasks"].setdefault(key, {})
        entry.update(payload)
        entry.setdefault("first_seen", now_in(self.tz_name).isoformat(sep=" ", timespec="seconds"))
        entry["last_seen"] = now_in(self.tz_name).isoformat(sep=" ", timespec="seconds")
        return entry

    # -------------------------------------------------------------- meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self.data.setdefault("meta", {}).get(key, default)

    def set_meta(self, key: str, value: Any) -> None:
        self.data.setdefault("meta", {})[key] = value

    # -------------------------------------------------------------- 清理

    def prune(self, keep_days: float = 14.0) -> int:
        """删除长期未出现且已过期的历史记录，控制文件体积。"""
        cutoff = now_in(self.tz_name) - timedelta(days=keep_days)
        removed = 0
        for key, entry in list(self.data["tasks"].items()):
            try:
                last_seen = datetime.fromisoformat(str(entry.get("last_seen")))
            except (TypeError, ValueError):
                last_seen = cutoff - timedelta(days=1)
            if last_seen < cutoff:
                self.data["tasks"].pop(key, None)
                removed += 1
        if removed:
            logger.info("状态清理：移除 %d 条历史记录", removed)
        return removed

    def stats(self) -> Dict[str, int]:
        return {"tracked": len(self.data["tasks"])}

    def all_keys(self) -> List[str]:
        return list(self.data["tasks"].keys())
