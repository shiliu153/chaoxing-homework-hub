"""口令哈希、会话令牌与敏感字段加密。

- 口令：`hashlib.scrypt`（N=2^14, r=8, p=1），存 `scrypt$N$r$p$salt_b64$hash_b64`；
- 会话令牌：随机 32 字节，库里只存 sha256，Cookie 里放原文；
- 账号密码落盘：优先用 `cryptography` 的 Fernet（密钥文件 0600），
  未安装时降级为明文并打警告（保证无依赖环境也能跑起来）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from typing import Optional, Tuple

logger = logging.getLogger("webapp.security")

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32


# ------------------------------------------------------------------ 口令


def hash_password(password: str) -> str:
    """生成口令哈希（自带随机盐）。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令，格式不对一律返回 False（不抛异常）。"""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            (password or "").encode("utf-8"), salt=salt,
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ 令牌


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """令牌只存指纹，库泄露也无法直接复用。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 敏感字段


class SecretBox:
    """账号密码等敏感字段的加解密；无 cryptography 时退化为明文标记。"""

    def __init__(self, path: str):
        self.path = path
        self._fernet = None
        self._load()

    def _load(self) -> None:
        try:
            from cryptography.fernet import Fernet  # type: ignore
        except ImportError:
            logger.warning(
                "未安装 cryptography，账号密码将以明文写入 600 权限的配置文件；"
                "建议 pip install cryptography 后重新保存一次设置"
            )
            return

        key = b""
        if os.path.isfile(self.path):
            try:
                key = open(self.path, "rb").read().strip()
            except OSError:
                key = b""
        if not key:
            key = Fernet.generate_key()
            self._atomic_write(key)
        self._fernet = Fernet(key)

    def _atomic_write(self, data: bytes) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        if not self._fernet:
            return "plain:" + value
        return "fernet:" + self._fernet.encrypt(value.encode("utf-8")).decode()

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("plain:"):
            return value[len("plain:"):]
        if value.startswith("fernet:") and self._fernet:
            try:
                return self._fernet.decrypt(value[len("fernet:"):].encode()).decode("utf-8")
            except Exception:  # noqa: BLE001 - 密钥变了就把密码当成未配置
                logger.warning("账号密码解密失败，可能是密钥文件被替换，请重新填写")
                return ""
        return value  # 兼容历史明文


# ------------------------------------------------------------------ 频率限制


class AttemptLimiter:
    """登录/注册的简单失败计数，避免在线爆破。"""

    def __init__(self, max_fails: int = 6, window: float = 300.0, lock: float = 120.0):
        self.max_fails = max_fails
        self.window = window
        self.lock_for = lock
        self._data: dict = {}

    def check(self, key: str) -> Tuple[bool, float]:
        """返回 (是否允许, 还需等待秒数)。"""
        import time

        now = time.time()
        fails, first, until = self._data.get(key, (0, now, 0.0))
        if until > now:
            return False, round(until - now, 1)
        if now - first > self.window:
            self._data[key] = (0, now, 0.0)
        return True, 0.0

    def fail(self, key: str) -> None:
        import time

        now = time.time()
        fails, first, _ = self._data.get(key, (0, now, 0.0))
        if now - first > self.window:
            fails, first = 0, now
        fails += 1
        until = now + self.lock_for if fails >= self.max_fails else 0.0
        self._data[key] = (fails, first, until)

    def reset(self, key: str) -> None:
        self._data.pop(key, None)


def mask_username(username: str) -> str:
    username = username or ""
    if len(username) >= 7:
        return username[:3] + "****" + username[-2:]
    if len(username) >= 4:
        return username[:2] + "***"
    return username


def describe_source(user: Optional[dict]) -> str:
    return "已登录" if user else "未登录"
