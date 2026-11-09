"""配置加载：YAML -> 强类型 dataclass，带默认值与合法性校验。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_IGNORE_STATUS = [
    "已交", "已提交", "已完成", "已批阅", "已结束", "已过期", "缺考",
    "待批阅", "已互评", "待互评", "已评分",
]
DEFAULT_PENDING_STATUS = [
    "未交", "未提交", "未完成", "待完成", "待做", "未开始", "进行中", "待考试",
    "待参加", "未参加", "考试中",
]


class ConfigError(ValueError):
    """配置文件不合法。"""


@dataclass
class AccountConfig:
    username: str = ""
    password: str = ""
    cookie: str = ""
    encrypt_password: bool = True
    # 部分学校需要 fid（机构号）；留空走普通登录
    fid: str = ""


@dataclass
class PollConfig:
    interval_seconds: int = 900
    jitter_seconds: int = 120
    daily_summary_at: str = ""  # 例如 "08:30"，留空表示不发每日汇总
    max_cycles: int = 0  # 0 表示无限循环


@dataclass
class RemindConfig:
    thresholds_hours: List[float] = field(default_factory=lambda: [72.0, 24.0, 3.0])
    notify_new: bool = True
    notify_overdue: bool = True
    only_pending: bool = False
    ignore_status_keywords: List[str] = field(
        default_factory=lambda: list(DEFAULT_IGNORE_STATUS)
    )
    pending_status_keywords: List[str] = field(
        default_factory=lambda: list(DEFAULT_PENDING_STATUS)
    )
    expire_grace_days: float = 30.0
    max_items_per_message: int = 30


@dataclass
class YuketangConfig:
    """长江雨课堂：只支持 Cookie 鉴权（浏览器里的 sessionid）。"""

    enabled: bool = False
    cookie: str = ""
    base_url: str = "https://changjiang.yuketang.cn"


@dataclass
class StorageConfig:
    data_dir: str = "data"
    state_file: str = "state.json"
    cookie_file: str = "cookies.json"
    summary_dir: str = "summaries"

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, self.state_file)

    @property
    def cookie_path(self) -> str:
        return os.path.join(self.data_dir, self.cookie_file)

    @property
    def summary_path(self) -> str:
        return os.path.join(self.data_dir, self.summary_dir)


@dataclass
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    poll: PollConfig = field(default_factory=PollConfig)
    remind: RemindConfig = field(default_factory=RemindConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    yuketang: YuketangConfig = field(default_factory=YuketangConfig)
    channels: List[Dict[str, Any]] = field(default_factory=lambda: [{"type": "console"}])
    timezone: str = "Asia/Shanghai"
    request_timeout: float = 15.0
    retry_times: int = 3
    log_level: str = "INFO"
    log_file: str = ""
    dry_run: bool = False
    source_dir: str = "."


def _pick(data: Dict[str, Any], key: str, default: Any) -> Any:
    value = data.get(key, default)
    return default if value is None else value


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.replace("，", ",").split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def load_config(path: str) -> Config:
    """读取 YAML 配置。相对路径统一以配置文件所在目录为基准。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("缺少依赖 pyyaml，请先执行：pip install pyyaml") from exc

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 语法错误（{path}）：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是映射（key: value）")

    base_dir = os.path.dirname(os.path.abspath(path))

    acc_raw = raw.get("account") or {}
    poll_raw = raw.get("poll") or {}
    remind_raw = raw.get("remind") or {}
    store_raw = raw.get("storage") or {}

    account = AccountConfig(
        username=str(_pick(acc_raw, "username", "")).strip(),
        password=str(_pick(acc_raw, "password", "")),
        cookie=str(_pick(acc_raw, "cookie", "")).strip(),
        encrypt_password=bool(_pick(acc_raw, "encrypt_password", True)),
        fid=str(_pick(acc_raw, "fid", "")).strip(),
    )
    # 环境变量优先，避免把口令写进配置文件
    account.username = os.environ.get("CX_USERNAME", account.username)
    account.password = os.environ.get("CX_PASSWORD", account.password)
    account.cookie = os.environ.get("CX_COOKIE", account.cookie)

    poll = PollConfig(
        interval_seconds=int(_pick(poll_raw, "interval_seconds", 900)),
        jitter_seconds=int(_pick(poll_raw, "jitter_seconds", 120)),
        daily_summary_at=str(_pick(poll_raw, "daily_summary_at", "") or ""),
        max_cycles=int(_pick(poll_raw, "max_cycles", 0)),
    )

    thresholds = _pick(remind_raw, "thresholds_hours", [72, 24, 3])
    try:
        thresholds_hours = sorted(
            {float(t) for t in thresholds if float(t) > 0}, reverse=True
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError("remind.thresholds_hours 必须是数字列表") from exc

    remind = RemindConfig(
        thresholds_hours=thresholds_hours or [24.0],
        notify_new=bool(_pick(remind_raw, "notify_new", True)),
        notify_overdue=bool(_pick(remind_raw, "notify_overdue", True)),
        only_pending=bool(_pick(remind_raw, "only_pending", False)),
        ignore_status_keywords=_as_list(
            _pick(remind_raw, "ignore_status_keywords", DEFAULT_IGNORE_STATUS)
        ),
        pending_status_keywords=_as_list(
            _pick(remind_raw, "pending_status_keywords", DEFAULT_PENDING_STATUS)
        ),
        expire_grace_days=float(_pick(remind_raw, "expire_grace_days", 30)),
        max_items_per_message=int(_pick(remind_raw, "max_items_per_message", 30)),
    )

    data_dir = str(_pick(store_raw, "data_dir", "data"))
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(base_dir, data_dir)
    storage = StorageConfig(
        data_dir=data_dir,
        state_file=str(_pick(store_raw, "state_file", "state.json")),
        cookie_file=str(_pick(store_raw, "cookie_file", "cookies.json")),
        summary_dir=str(_pick(store_raw, "summary_dir", "summaries")),
    )

    yt_raw = raw.get("yuketang") or {}
    yt_cookie = os.environ.get("CX_YT_COOKIE", str(_pick(yt_raw, "cookie", ""))).strip()
    yuketang = YuketangConfig(
        cookie=yt_cookie,
        base_url=str(_pick(yt_raw, "base_url", "https://changjiang.yuketang.cn")).rstrip("/"),
        # 配了 Cookie 就默认启用，除非显式关掉
        enabled=bool(_pick(yt_raw, "enabled", bool(yt_cookie))) and bool(yt_cookie),
    )

    channels = raw.get("notify") or raw.get("channels") or [{"type": "console"}]
    if isinstance(channels, dict):
        channels = channels.get("channels") or [channels]
    if not isinstance(channels, list):
        raise ConfigError("notify.channels 必须是列表")
    norm_channels: List[Dict[str, Any]] = []
    for item in channels:
        if isinstance(item, str):
            norm_channels.append({"type": item})
        elif isinstance(item, dict) and item.get("type"):
            norm_channels.append(dict(item))
    if not norm_channels:
        norm_channels = [{"type": "console"}]

    logging_raw = raw.get("logging") or {}
    log_file = str(_pick(logging_raw, "file", "") or "")
    if log_file and not os.path.isabs(log_file):
        log_file = os.path.join(base_dir, log_file)

    if poll.interval_seconds < 30:
        raise ConfigError("poll.interval_seconds 不能小于 30 秒，避免请求过密")
    if poll.daily_summary_at:
        try:
            hour, minute = poll.daily_summary_at.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError
        except ValueError as exc:
            raise ConfigError("poll.daily_summary_at 格式应为 HH:MM") from exc

    return Config(
        account=account,
        poll=poll,
        remind=remind,
        storage=storage,
        yuketang=yuketang,
        channels=norm_channels,
        timezone=str(_pick(raw, "timezone", "Asia/Shanghai")),
        request_timeout=float(_pick(raw, "request_timeout", 15)),
        retry_times=int(_pick(raw, "retry_times", 3)),
        log_level=str(_pick(logging_raw, "level", "INFO")).upper(),
        log_file=log_file,
        dry_run=bool(_pick(raw, "dry_run", False)),
        source_dir=base_dir,
    )


def validate_account(cfg: Config) -> None:
    """登录前检查凭据是否齐备。"""
    if cfg.account.cookie:
        return
    if not cfg.account.username or not cfg.account.password:
        raise ConfigError(
            "未配置账号密码：请在配置文件的 account.username / account.password 填写，"
            "或设置环境变量 CX_USERNAME / CX_PASSWORD，亦可直接提供 account.cookie"
        )
