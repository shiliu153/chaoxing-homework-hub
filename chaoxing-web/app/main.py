"""Web API：前后端分离后的纯接口服务（多用户）。

职责边界：
- 本服务只提供 `/api/v1/**` 的 JSON 接口，不再托管页面；
  前端静态资源由 nginx（或任意静态服务器）独立部署。
- 身份：Cookie 里的会话令牌（也支持 `Authorization: Bearer`），
  每个请求先解析出 user_id，再用 `EngineManager.engine(user_id)` 取该用户自己的引擎。
- 隔离：配置、状态、缓存、汇总、日志全部落在 `data/users/<user_id>/`，
  接口层不接受任何路径参数，因此不存在跨用户读写。
"""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .security import AttemptLimiter, mask_username, new_token
from .store import UserStore
from .tenancy import EngineManager, Tenancy

logger = logging.getLogger("webapp")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DATA_ROOT = os.path.abspath(os.environ.get("CX_DATA_DIR", os.path.join(PROJECT_DIR, "data")))
LEGACY_CONFIG = os.environ.get("CX_LEGACY_CONFIG", os.path.join(PROJECT_DIR, "config.yaml"))
FRONTEND_DIR = os.environ.get("CX_FRONTEND_DIR", os.path.join(PROJECT_DIR, "web"))
COOKIE_NAME = "cx_session"
SESSION_DAYS = 30
ALLOW_REGISTRATION = os.environ.get("CX_ALLOW_REGISTRATION", "1") != "0"
MAX_CONCURRENT_POLLS = int(os.environ.get("CX_MAX_CONCURRENT_POLLS", "2") or 2)
CORS_ORIGINS = [o.strip() for o in os.environ.get("CX_CORS_ORIGINS", "").split(",") if o.strip()]

STORE: Optional[UserStore] = None
TENANCY: Optional[Tenancy] = None
MANAGER: Optional[EngineManager] = None

LIMITER = AttemptLimiter()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fa5]{3,32}$")


# ------------------------------------------------------------------ 模型


class RegisterPayload(BaseModel):
    """注册只收用户名和密码，昵称默认等于用户名。"""
    username: str
    password: str


class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


class PasswordPayload(BaseModel):
    old_password: str
    new_password: str


class IgnorePayload(BaseModel):
    key: str
    title: Optional[str] = None
    course_name: Optional[str] = None
    kind: Optional[str] = None
    source: Optional[str] = None
    status: Optional[str] = None
    deadline: Optional[str] = None


class KeyPayload(BaseModel):
    key: str


class SettingsPayload(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    cookie: Optional[str] = None
    interval_seconds: Optional[int] = None
    thresholds_hours: Optional[List[float]] = None
    expire_grace_days: Optional[float] = None
    daily_summary_at: Optional[str] = None
    wecom_webhook: Optional[str] = None
    serverchan_key: Optional[str] = None
    yuketang_cookie: Optional[str] = None
    yuketang_enabled: Optional[bool] = None
    dry_run: Optional[bool] = None


# ------------------------------------------------------------------ 生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STORE, TENANCY, MANAGER

    os.makedirs(DATA_ROOT, exist_ok=True)
    STORE = UserStore(os.path.join(DATA_ROOT, "app.db"))
    TENANCY = Tenancy(DATA_ROOT, legacy_config=LEGACY_CONFIG)
    MANAGER = EngineManager(TENANCY, max_concurrent_polls=MAX_CONCURRENT_POLLS)

    migrated = TENANCY.migrate_legacy(STORE)
    if migrated:
        logger.warning(
            "检测到单用户历史数据，已迁移为账号 %s（口令沿用原来的网页访问口令）",
            migrated["username"],
        )

    users = [u for u in STORE.list_users() if u.get("status") == "active"]
    for user in users:
        TENANCY.ensure_workspace(int(user["id"]))
    MANAGER.start_all([int(u["id"]) for u in users])
    STORE.purge_expired()
    logger.info("多用户服务启动：%d 个用户，数据根目录 %s", len(users), DATA_ROOT)
    try:
        yield
    finally:
        if MANAGER:
            MANAGER.stop_all()
        logger.info("服务停止")


app = FastAPI(title="学习通待办中心 API", docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json", lifespan=lifespan)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ------------------------------------------------------------------ 依赖


def store() -> UserStore:
    if STORE is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return STORE


def manager() -> EngineManager:
    if MANAGER is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return MANAGER


def tenancy() -> Tenancy:
    if TENANCY is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return TENANCY


def _token_from(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        return token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def current_user(request: Request) -> Dict[str, Any]:
    user = store().resolve_session(_token_from(request))
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_cookie(request: Request, response: Response, token: str) -> None:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=(proto == "https"),
        path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
    }


# ------------------------------------------------------------------ 健康检查


@app.get("/api/health")
@app.get("/api/v1/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "chaoxing-reminder-api", "version": "2.0.0", "mode": "multi-user"}


# ------------------------------------------------------------------ 认证


@app.post("/api/v1/auth/register")
def register(payload: RegisterPayload, request: Request, response: Response) -> Dict[str, Any]:
    if not ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="站点已关闭注册，请联系管理员开通账号")
    username = (payload.username or "").strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="用户名需 3-32 位，可用中英文、数字、下划线、点、横线")
    if len(payload.password or "") < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")

    ip = _client_ip(request)
    allowed, wait = LIMITER.check(f"reg:{ip}")
    if not allowed:
        raise HTTPException(status_code=429, detail=f"操作过于频繁，请 {int(wait)} 秒后再试")

    if store().get_by_username(username):
        raise HTTPException(status_code=409, detail="该用户名已被占用")

    try:
        user = store().create_user(username, payload.password)
    except Exception as exc:  # noqa: BLE001 - 唯一约束等
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from exc

    tenancy().ensure_workspace(int(user["id"]))
    manager().start_user(int(user["id"]))

    token = new_token()
    store().create_session(int(user["id"]), token, days=SESSION_DAYS,
                           user_agent=request.headers.get("user-agent", ""))
    _set_cookie(request, response, token)
    logger.info("新用户注册：%s（id=%s, ip=%s）", username, user["id"], ip)
    return {"ok": True, "user": _public_user(user), "need_bind": True}


@app.post("/api/v1/auth/login")
def login(payload: LoginPayload, request: Request, response: Response) -> Dict[str, Any]:
    username = (payload.username or "").strip()
    ip = _client_ip(request)
    keys = (f"login:{ip}", f"login:{username.lower()}")
    for key in keys:
        allowed, wait = LIMITER.check(key)
        if not allowed:
            raise HTTPException(status_code=429, detail=f"失败次数过多，请 {int(wait)} 秒后再试")

    user = store().authenticate(username, payload.password or "")
    if not user:
        for key in keys:
            LIMITER.fail(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    for key in keys:
        LIMITER.reset(key)

    token = new_token()
    store().create_session(int(user["id"]), token, days=SESSION_DAYS,
                           user_agent=request.headers.get("user-agent", ""))
    _set_cookie(request, response, token)
    eng = manager().engine(int(user["id"]))
    return {
        "ok": True,
        "user": _public_user(user),
        "account_ready": _account_ready(eng),
    }


@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    token = _token_from(request)
    if token:
        store().revoke_session(token)
    _clear_cookie(response)
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    eng = manager().engine(int(user["id"]))
    return {
        "authenticated": True,
        "user": _public_user(user),
        "account_ready": _account_ready(eng),
        "bindings": {
            "chaoxing": bool(eng.cfg.account.cookie
                             or (eng.cfg.account.username and eng.cfg.account.password)),
            "yuketang": bool(eng.cfg.yuketang.enabled and eng.cfg.yuketang.cookie),
        },
    }


@app.post("/api/v1/auth/password")
def change_password(payload: PasswordPayload, response: Response,
                    user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if not store().verify_user_password(int(user["id"]), payload.old_password):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if len(payload.new_password or "") < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    store().set_password(int(user["id"]), payload.new_password)
    _clear_cookie(response)
    return {"ok": True, "message": "密码已修改，请重新登录"}


def _account_ready(eng) -> bool:
    return bool(
        eng.cfg.account.cookie
        or (eng.cfg.account.username and eng.cfg.account.password)
        or (eng.cfg.yuketang.enabled and eng.cfg.yuketang.cookie)
    )


# ------------------------------------------------------------------ 数据


@app.get("/api/v1/overview")
def overview(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return manager().engine(int(user["id"])).overview()


@app.post("/api/v1/refresh")
def refresh(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    started = manager().engine(int(user["id"])).trigger_async()
    return {"started": started, "message": "已开始检测" if started else "已有检测在进行中"}


@app.get("/api/v1/events")
def events(limit: int = 50, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"items": manager().engine(int(user["id"])).read_events(max(1, min(limit, 200)))}


@app.get("/api/v1/tasks/{key}")
def task_detail(key: str, refresh: int = 0,
                user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        return manager().engine(int(user["id"])).task_detail(key, refresh=bool(refresh))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc


@app.post("/api/v1/ignore")
def ignore(payload: IgnorePayload, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    item = manager().engine(int(user["id"])).ignore_task(payload.key, payload.model_dump())
    return {"ok": True, "item": item}


@app.post("/api/v1/unignore")
def unignore(payload: KeyPayload, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    ok = manager().engine(int(user["id"])).unignore_task(payload.key)
    return {"ok": ok}


@app.get("/api/v1/logs")
def logs(lines: int = 120, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    eng = manager().engine(int(user["id"]))
    path = eng.cfg.log_file
    if not path or not os.path.isfile(path):
        return {"lines": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()[-max(1, min(lines, 500)):]
    except OSError as exc:
        return {"lines": [f"读取日志失败：{exc}"]}
    return {"lines": [line.rstrip() for line in data]}


# ------------------------------------------------------------------ 设置

def _read_config(user_id: int) -> Dict[str, Any]:
    return tenancy().read_raw(user_id)


def _write_config(user_id: int, raw: Dict[str, Any]) -> None:
    tenancy().write_raw(user_id, raw)


@app.get("/api/v1/settings")
def get_settings(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    uid = int(user["id"])
    eng = manager().engine(uid)
    raw = _read_config(uid)
    channels = raw.get("notify", {}).get("channels") or []
    wecom = next((c.get("webhook", "") for c in channels if c.get("type") == "wecom"), "")
    serverchan = next((c.get("send_key", "") for c in channels if c.get("type") == "serverchan"), "")
    username = raw.get("account", {}).get("username", "") or ""
    return {
        "username": username,
        "username_masked": mask_username(username),
        "has_password": bool(raw.get("account", {}).get("password")),
        "has_cookie": bool(raw.get("account", {}).get("cookie")),
        "interval_seconds": eng.cfg.poll.interval_seconds,
        "expire_grace_days": eng.cfg.remind.expire_grace_days,
        "thresholds_hours": eng.cfg.remind.thresholds_hours,
        "daily_summary_at": raw.get("poll", {}).get("daily_summary_at", ""),
        "wecom_webhook": wecom,
        "serverchan_key": serverchan,
        "dry_run": eng.cfg.dry_run,
        "yuketang_enabled": bool(eng.cfg.yuketang.enabled),
        "yuketang_has_cookie": bool(eng.cfg.yuketang.cookie),
        "yuketang_cookie_tail": (eng.cfg.yuketang.cookie[-8:]
                                 if eng.cfg.yuketang.cookie else ""),
        "channels": [c.get("type") for c in channels],
    }


@app.post("/api/v1/settings")
def update_settings(payload: SettingsPayload,
                    user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    uid = int(user["id"])
    raw = _read_config(uid)
    raw.setdefault("account", {})
    raw.setdefault("poll", {})
    raw.setdefault("remind", {})
    raw.setdefault("notify", {})

    if payload.username is not None:
        raw["account"]["username"] = payload.username.strip()
    if payload.password:
        raw["account"]["password"] = payload.password
    if payload.cookie is not None:
        raw["account"]["cookie"] = payload.cookie.strip()
    if payload.interval_seconds is not None:
        if payload.interval_seconds < 60:
            raise HTTPException(status_code=400, detail="轮询间隔不能小于 60 秒")
        raw["poll"]["interval_seconds"] = int(payload.interval_seconds)
    if payload.daily_summary_at is not None:
        raw["poll"]["daily_summary_at"] = payload.daily_summary_at.strip()
    if payload.thresholds_hours is not None:
        values = sorted({float(v) for v in payload.thresholds_hours if float(v) > 0}, reverse=True)
        raw["remind"]["thresholds_hours"] = values or [24]
    if payload.expire_grace_days is not None:
        days = float(payload.expire_grace_days)
        if days < 0 or days > 3650:
            raise HTTPException(status_code=400, detail="逾期保留天数需在 0-3650 之间")
        raw["remind"]["expire_grace_days"] = days
    if payload.dry_run is not None:
        raw["dry_run"] = bool(payload.dry_run)

    if payload.yuketang_cookie is not None or payload.yuketang_enabled is not None:
        raw.setdefault("yuketang", {})
        if payload.yuketang_cookie:
            raw["yuketang"]["cookie"] = payload.yuketang_cookie.strip()
        if payload.yuketang_enabled is not None:
            raw["yuketang"]["enabled"] = bool(payload.yuketang_enabled)

    if payload.wecom_webhook is not None or payload.serverchan_key is not None:
        old = {c.get("type"): c for c in (raw["notify"].get("channels") or [])}
        channels: List[Dict[str, Any]] = [old.get("console") or {"type": "console"}]
        wecom = payload.wecom_webhook if payload.wecom_webhook is not None else (
            old.get("wecom", {}).get("webhook", "")
        )
        if wecom:
            channels.append({"type": "wecom", "webhook": wecom.strip()})
        sc = payload.serverchan_key if payload.serverchan_key is not None else (
            old.get("serverchan", {}).get("send_key", "")
        )
        if sc:
            channels.append({"type": "serverchan", "send_key": sc.strip()})
        for kind, item in old.items():
            if kind not in ("console", "wecom", "serverchan"):
                channels.append(item)
        raw["notify"]["channels"] = channels

    _write_config(uid, raw)
    manager().engine(uid).reload()
    return {"ok": True}


# ------------------------------------------------------------------ 雨课堂扫码


_YT_LOGIN: Dict[str, Dict[str, Any]] = {}
_YT_BASE = "https://changjiang.yuketang.cn"
_YT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _yt_session():
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": _YT_UA,
        "xtbz": "ykt",
        "Accept": "application/json, text/plain, */*",
        "Referer": _YT_BASE + "/v2/web/index",
    })
    return session


@app.post("/api/v1/yuketang/login/start")
def yt_login_start(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    import urllib.parse
    import uuid as _uuid

    session = _yt_session()
    try:
        resp = session.post(_YT_BASE + "/api/v3/user/login/wechat-auth-param", timeout=20)
        payload = resp.json().get("data") or {}
        appid, state, redirect = payload.get("appId"), payload.get("state"), payload.get("redirectUri")
        if not (appid and state and redirect):
            raise RuntimeError("未取到微信登录参数")

        qr_url = (
            "https://open.weixin.qq.com/connect/qrconnect?appid=%s&scope=snsapi_login"
            "&redirect_uri=%s&state=%s&login_type=jssdk&self_redirect=default&style=black"
            % (appid, urllib.parse.quote(redirect, safe=""), state)
        )
        page = session.get(qr_url, timeout=20).text
        m = re.search(r"/connect/qrcode/([A-Za-z0-9_\-]+)", page)
        if not m:
            raise RuntimeError("二维码解析失败，请稍后重试")
        qr_uuid = m.group(1)
        png = session.get(
            "https://open.weixin.qq.com/connect/qrcode/%s" % qr_uuid, timeout=20
        ).content
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"生成二维码失败：{exc}") from exc

    sid = _uuid.uuid4().hex[:12]
    _YT_LOGIN[sid] = {
        "user_id": int(user["id"]),      # 归属校验：只能由发起人轮询
        "session": session,
        "uuid": qr_uuid,
        "state": state,
        "redirect": redirect,
        "png": png,
        "created": time.time(),
        "status": "waiting",
        "user": "",
    }
    for key in sorted(_YT_LOGIN, key=lambda k: _YT_LOGIN[k]["created"])[:-5]:
        _YT_LOGIN.pop(key, None)
    return {"id": sid, "expires_in": 300}


def _yt_item(sid: str, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item = _YT_LOGIN.get(sid)
    if not item or item.get("user_id") != int(user["id"]):
        return None
    return item


@app.get("/api/v1/yuketang/login/qr")
def yt_login_qr(id: str, user: Dict[str, Any] = Depends(current_user)) -> Response:
    item = _yt_item(id, user)
    if not item:
        raise HTTPException(status_code=404, detail="二维码已失效，请重新获取")
    return Response(content=item["png"], media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/v1/yuketang/login/status")
def yt_login_status(id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    uid = int(user["id"])
    item = _yt_item(id, user)
    if not item:
        return {"status": "expired", "detail": "会话不存在，请重新获取二维码"}

    if item["status"] == "ok":
        return {"status": "ok", "user": item.get("user", "")}

    if time.time() - item["created"] > 290:
        item["status"] = "expired"
        return {"status": "expired", "detail": "二维码已过期"}

    session = item["session"]
    try:
        resp = session.get(
            "https://lp.open.weixin.qq.com/connect/l/qrconnect",
            params={"uuid": item["uuid"], "_": int(time.time() * 1000)},
            timeout=10,
            headers={"Referer": "https://open.weixin.qq.com/"},
        )
        text = resp.text
    except Exception:  # noqa: BLE001
        return {"status": "waiting", "detail": "等待扫码"}

    m_code = re.search(r"wx_code='([^']*)'", text)
    code = m_code.group(1) if m_code else ""
    if not code:
        errcode = re.search(r"wx_errcode=(\d+)", text)
        if errcode and errcode.group(1) == "405":
            item["status"] = "expired"
            return {"status": "expired", "detail": "二维码已过期"}
        if errcode and errcode.group(1) in ("404", "403"):
            return {"status": "scanned", "detail": "已扫码，请在手机上确认"}
        return {"status": "waiting", "detail": "等待扫码"}

    try:
        session.get(
            item["redirect"],
            params={"path": "/v2/web/index", "code": code, "state": item["state"]},
            allow_redirects=False,
            timeout=20,
        )
        info = session.get(_YT_BASE + "/api/v3/user/basic-info", timeout=20).json()
    except Exception as exc:  # noqa: BLE001
        item["status"] = "failed"
        return {"status": "failed", "detail": f"换取登录态失败：{exc}"}

    if not info.get("data"):
        item["status"] = "failed"
        return {"status": "failed", "detail": "登录校验失败，请重新扫码"}

    jar = {c.name: c.value for c in session.cookies}
    keep = ("sessionid", "csrftoken", "universalAuthToken", "ucid", "uname")
    cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items() if k in keep)
    if "sessionid" not in jar:
        item["status"] = "failed"
        return {"status": "failed", "detail": "未获取到 sessionid，请重新扫码"}

    raw = _read_config(uid)
    raw.setdefault("yuketang", {})
    raw["yuketang"]["enabled"] = True
    raw["yuketang"]["cookie"] = cookie_str
    raw["yuketang"]["base_url"] = _YT_BASE
    _write_config(uid, raw)
    manager().engine(uid).reload()

    name = str((info.get("data") or {}).get("name") or "")
    item["status"] = "ok"
    item["user"] = name
    logger.info("用户 %s 雨课堂扫码登录成功：%s", uid, name)
    return {"status": "ok", "user": name}


# ------------------------------------------------------------------ 诊断 / 测试


@app.post("/api/v1/diagnose")
def diagnose(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """分步诊断：学习通（账号/登录/课程/抽样） + 雨课堂（Cookie/考试）。"""
    from cxreminder.client import ChaoxingClient
    from cxreminder.utils import now_in
    from cxreminder.yuketang import YuketangClient, _pick

    eng = manager().engine(int(user["id"]))
    cfg = eng.cfg
    steps: List[Dict[str, Any]] = []
    ok_all = True

    def add(name: str, ok: bool, detail: str, cost: float = 0.0) -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        steps.append({"name": name, "ok": ok, "detail": detail, "cost": round(cost, 1)})

    cx_ready = bool(cfg.account.cookie or (cfg.account.username and cfg.account.password))
    yt_ready = bool(cfg.yuketang.enabled and cfg.yuketang.cookie)

    if not cx_ready and not yt_ready:
        add("账号配置", False, "既没有绑定学习通账号，也没有绑定雨课堂")
        return {"ok": False, "steps": steps}

    if not cx_ready:
        add("学习通", True, "未绑定，已跳过")
    else:
        add("学习通账号", True, mask_username(cfg.account.username) or "Cookie 登录")
        client = ChaoxingClient(cfg)
        t0 = time.time()
        try:
            client.login()
            add("学习通登录", True, "登录态有效", time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            add("学习通登录", False, str(exc)[:300], time.time() - t0)
            client = None
        if client is not None:
            t0 = time.time()
            try:
                courses = client.fetch_courses()
                add("学习通课程", True, f"{len(courses)} 门课程", time.time() - t0)
                now = now_in(cfg.timezone)
                for course in courses[:2]:
                    t0 = time.time()
                    try:
                        tasks = client.fetch_course_tasks(course)
                    except Exception as exc:  # noqa: BLE001
                        add(f"学习通《{course.name[:12]}》", False, str(exc)[:200],
                            time.time() - t0)
                        continue
                    pending = [t for t in tasks if t.is_pending(cfg.remind, now)]
                    detail = f"共 {len(tasks)} 条，待办 {len(pending)} 条"
                    if tasks:
                        detail += "；示例：" + tasks[0].title[:20]
                    add(f"学习通《{course.name[:12]}》", True, detail, time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                add("学习通课程", False, str(exc)[:300], time.time() - t0)

    if not yt_ready:
        add("雨课堂", True, "未绑定，已跳过")
    else:
        yt = YuketangClient(cfg)
        add("雨课堂配置", True, f"Cookie 尾号 …{cfg.yuketang.cookie[-6:]}")
        t0 = time.time()
        try:
            info = yt.login()
            name = _pick(info, "name", "nickname", "school_number", default="已登录")
            add("雨课堂登录", True, str(name)[:40], time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            add("雨课堂登录", False, str(exc)[:300], time.time() - t0)
            yt = None
        if yt is not None:
            t0 = time.time()
            try:
                courses = yt.fetch_courses()
                add("雨课堂课程", True, f"{len(courses)} 门课程", time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                add("雨课堂课程", False, str(exc)[:300], time.time() - t0)

            t0 = time.time()
            try:
                items = yt.fetch_exam_items()
                exams = [x for x in items
                         if str(_pick(x, "type", "item_type", default="")).lower()
                         in ("", "exam", "exam_paper")]
                detail = f"原始 {len(items)} 条，其中考试 {len(exams)} 场"
                if exams:
                    detail += "；示例：" + str(_pick(exams[0], "title", "name", default=""))[:20]
                add("雨课堂考试列表", True, detail, time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                add("雨课堂考试列表", False, str(exc)[:300], time.time() - t0)

            t0 = time.time()
            try:
                pending = yt.collect()
                detail = f"待参加 {len(pending)} 场"
                if pending:
                    first = pending[0]
                    detail += (f"；最近一场：{first.title[:18]}，截止 "
                               f"{first.deadline.strftime('%m-%d %H:%M') if first.deadline else '未知'}")
                add("雨课堂待参加考试", True, detail, time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                add("雨课堂待参加考试", False, str(exc)[:300], time.time() - t0)

    add("结论", ok_all, "连接正常" if ok_all else "存在异常，详见上方步骤")
    return {"ok": ok_all, "steps": steps}


@app.post("/api/v1/notify/test")
def test_notify(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    from cxreminder.notify import Notifier

    eng = manager().engine(int(user["id"]))
    notifier = Notifier(eng.cfg.channels, dry_run=eng.cfg.dry_run, timeout=eng.cfg.request_timeout)
    results = notifier.send(
        "学习通提醒：通道测试",
        f"### 这是一条测试消息\n\n- 时间：{eng.overview()['now']}\n- 收到即表示通道配置正确。",
    )
    return {
        "results": [{"channel": n, "ok": ok, "detail": detail} for n, ok, detail in results]
    }


# ------------------------------------------------------------------ 前端（可选）

if os.environ.get("CX_SERVE_FRONTEND") == "1" and os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    logger.info("已挂载前端静态目录：%s", FRONTEND_DIR)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
