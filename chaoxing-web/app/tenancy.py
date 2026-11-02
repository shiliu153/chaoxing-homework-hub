"""多租户目录与配置：每个用户一套独立的目录、配置与引擎。

目录结构：

    <data_root>/
      app.db                 # 用户 + 会话（SQLite）
      secret.key             # 敏感字段加密密钥（0600）
      users/
        <user_id>/
          config.yaml        # 该用户的学习通/雨课堂账号、阈值、推送通道
          state.json         # 去重状态
          ignored.json       # 忽略清单
          cookies.json       # 学习通登录态缓存
          task_cache/        # 作业详情缓存
          summaries/         # 每轮汇总
          events.jsonl       # 提醒历史
          reminder.log       # 采集日志

隔离原则：所有读写都经过 `user_dir(user_id)`，接口层不允许传入任意路径，
因此 A 用户的请求不可能读到 B 用户的数据。
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import threading
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger("webapp.tenancy")

LEGACY_FILES = ("state.json", "ignored.json", "cookies.json", "events.jsonl")
LEGACY_DIRS = ("task_cache", "summaries")


def default_config(user_dir: str) -> Dict[str, Any]:
    """新用户的初始配置：账号为空，等用户自己绑定。"""
    return {
        "account": {
            "username": "",
            "password": "",
            "cookie": "",
            "fid": "",
            "encrypt_password": True,
        },
        "poll": {
            "interval_seconds": 900,
            "jitter_seconds": 60,
            "daily_summary_at": "",
            "max_cycles": 0,
        },
        "remind": {"thresholds_hours": [72, 24, 3]},
        "notify": {"channels": [{"type": "console"}]},
        "yuketang": {
            "enabled": False,
            "cookie": "",
            "base_url": "https://changjiang.yuketang.cn",
        },
        "storage": {"data_dir": user_dir},
        "logging": {"level": "INFO", "file": os.path.join(user_dir, "reminder.log")},
        "dry_run": False,
    }


class Tenancy:
    def __init__(self, data_root: str, legacy_config: str = ""):
        self.data_root = os.path.abspath(data_root)
        self.users_root = os.path.join(self.data_root, "users")
        self.legacy_config = legacy_config or os.path.join(self.data_root, "config.yaml")
        os.makedirs(self.users_root, exist_ok=True)
        try:
            os.chmod(self.users_root, 0o700)
        except OSError:
            pass

    # ------------------------------------------------------------ 路径

    def user_dir(self, user_id: int) -> str:
        return os.path.join(self.users_root, str(int(user_id)))

    def config_path(self, user_id: int) -> str:
        return os.path.join(self.user_dir(user_id), "config.yaml")

    def ensure_workspace(self, user_id: int) -> str:
        """创建用户目录与初始配置，返回配置路径。"""
        path = self.user_dir(user_id)
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        cfg_path = self.config_path(user_id)
        if not os.path.isfile(cfg_path):
            self.write_raw(user_id, default_config(path))
        return cfg_path

    # ------------------------------------------------------------ 配置读写

    def read_raw(self, user_id: int) -> Dict[str, Any]:
        cfg_path = self.ensure_workspace(user_id)
        with open(cfg_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def write_raw(self, user_id: int, raw: Dict[str, Any]) -> None:
        path = self.user_dir(user_id)
        os.makedirs(path, exist_ok=True)
        cfg_path = self.config_path(user_id)
        # storage.data_dir 永远是本用户目录，避免被前端改到别人目录
        raw.setdefault("storage", {})
        raw["storage"]["data_dir"] = path
        raw.setdefault("logging", {})
        if not raw["logging"].get("file"):
            raw["logging"]["file"] = os.path.join(path, "reminder.log")
        tmp = f"{cfg_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg_path)

    # ------------------------------------------------------------ 历史数据迁移

    def migrate_legacy(self, store) -> Optional[Dict[str, str]]:
        """把单用户版本的数据迁到 users/1/，并建一个 admin 账号。

        口令沿用原来的网页访问口令（data/web_password），这样老用户不用改习惯。
        返回 {"username", "password"} 便于写日志提示。
        """
        if store.count() > 0:
            return None
        if not os.path.isfile(self.legacy_config):
            return None

        password = ""
        pwd_file = os.path.join(self.data_root, "web_password")
        if os.path.isfile(pwd_file):
            try:
                password = open(pwd_file, "r", encoding="utf-8").read().strip()
            except OSError:
                password = ""
        if not password:
            password = secrets.token_urlsafe(9)

        user = store.create_user("admin", password, "管理员")
        user_id = int(user["id"])
        self.ensure_workspace(user_id)
        user_path = self.user_dir(user_id)

        # 1) 复制配置并改写存储路径
        try:
            with open(self.legacy_config, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        if raw:
            self.write_raw(user_id, raw)

        # 2) 搬历史数据
        for name in LEGACY_FILES:
            src = os.path.join(self.data_root, name)
            if os.path.isfile(src):
                try:
                    shutil.move(src, os.path.join(user_path, name))
                except OSError as exc:
                    logger.warning("迁移 %s 失败：%s", name, exc)
        for name in LEGACY_DIRS:
            src = os.path.join(self.data_root, name)
            if os.path.isdir(src):
                dst = os.path.join(user_path, name)
                try:
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    shutil.move(src, dst)
                except OSError as exc:
                    logger.warning("迁移 %s 失败：%s", name, exc)

        # 3) 老配置改名，避免下次启动重复迁移
        try:
            os.replace(self.legacy_config, self.legacy_config + ".migrated")
        except OSError:
            pass

        logger.info("已迁移单用户数据到 users/%s（登录名 admin）", user_id)
        return {"username": "admin", "password": password}


class EngineManager:
    """按用户懒加载 Engine，并负责各自的定时线程。"""

    def __init__(self, tenancy: Tenancy, tz_name: str = "Asia/Shanghai",
                 max_concurrent_polls: int = 2):
        self.tenancy = tenancy
        self.tz_name = tz_name
        self._engines: Dict[int, Any] = {}
        self._lock = threading.RLock()
        # 所有用户共享：同一时刻最多 N 个采集在跑
        self.poll_slots = threading.Semaphore(max(1, int(max_concurrent_polls)))

    def engine(self, user_id: int):
        from .engine import Engine

        uid = int(user_id)
        with self._lock:
            eng = self._engines.get(uid)
            if eng is None:
                cfg_path = self.tenancy.ensure_workspace(uid)
                eng = Engine(cfg_path, tz_name=self.tz_name,
                             run_semaphore=self.poll_slots)
                self._engines[uid] = eng
            return eng

    def start_user(self, user_id: int) -> None:
        try:
            self.engine(user_id).start_scheduler()
        except Exception:  # noqa: BLE001 - 单个用户起不来不影响其他人
            logger.exception("启动用户 %s 的采集线程失败", user_id)

    def start_all(self, user_ids) -> None:
        for uid in user_ids:
            self.start_user(uid)

    def stop_all(self) -> None:
        with self._lock:
            for eng in self._engines.values():
                try:
                    eng.stop_scheduler()
                except Exception:  # noqa: BLE001
                    pass
            self._engines.clear()
