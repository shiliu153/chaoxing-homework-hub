"""极简登录鉴权：HMAC 签名的 HttpOnly Cookie + 登录失败限流。

站点对外暴露，且后台持有学习通账号凭据，所以默认强制口令保护：
口令来自环境变量 CX_WEB_PASSWORD；未设置时启动自动生成并写入 data/web_password。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Dict, Tuple

from fastapi import HTTPException, Request, Response

COOKIE_NAME = "cx_session"
SESSION_TTL = 7 * 24 * 3600  # 7 天

_LOGIN_FAILS: Dict[str, Tuple[int, float]] = {}
_MAX_FAILS = 5
_FAIL_WINDOW = 600


def _load_secret(data_dir: str) -> str:
    env = os.environ.get("CX_WEB_SECRET")
    if env:
        return env
    path = os.path.join(data_dir, "web_secret")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    value = secrets.token_urlsafe(32)
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def _load_password(data_dir: str) -> str:
    env = os.environ.get("CX_WEB_PASSWORD")
    if env:
        return env
    path = os.path.join(data_dir, "web_password")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    value = secrets.token_urlsafe(9)
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"[auth] 已生成初始访问口令：{value}（保存在 {path}）", flush=True)
    return value


class Auth:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.secret = _load_secret(data_dir).encode("utf-8")
        self.password = _load_password(data_dir)

    # ---------------------------------------------------------- token

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_TTL
        payload = str(expires)
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> bool:
        try:
            payload, signature = token.split(".", 1)
            if int(payload) < time.time():
                return False
        except (ValueError, AttributeError):
            return False
        return hmac.compare_digest(self._sign(payload), signature)

    # ---------------------------------------------------------- 口令

    def check_password(self, password: str) -> bool:
        return hmac.compare_digest(password.encode("utf-8"), self.password.encode("utf-8"))

    def set_password(self, new_password: str) -> None:
        if len(new_password) < 6:
            raise ValueError("口令至少 6 位")
        self.password = new_password

    def persist(self) -> None:
        """把当前口令落盘，重启后继续生效。"""
        path = os.path.join(self.data_dir, "web_password")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.password)
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ---------------------------------------------------------- 限流

    def guard(self, ip: str) -> None:
        fails, first = _LOGIN_FAILS.get(ip, (0, 0.0))
        if fails >= _MAX_FAILS and time.time() - first < _FAIL_WINDOW:
            raise HTTPException(status_code=429, detail="尝试过于频繁，请 10 分钟后再试")

    def note_fail(self, ip: str) -> None:
        fails, first = _LOGIN_FAILS.get(ip, (0, 0.0))
        if time.time() - first > _FAIL_WINDOW:
            fails, first = 0, time.time()
        _LOGIN_FAILS[ip] = (fails + 1, first)

    def note_success(self, ip: str) -> None:
        _LOGIN_FAILS.pop(ip, None)

    # ---------------------------------------------------------- 依赖

    def require(self, request: Request) -> None:
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or not self.verify(token):
            raise HTTPException(status_code=401, detail="未登录")

    def set_cookie(self, response: Response, token: str, secure: bool) -> None:
        response.set_cookie(
            COOKIE_NAME, token, max_age=SESSION_TTL, httponly=True,
            samesite="lax", secure=secure, path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/")
