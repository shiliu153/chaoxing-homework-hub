"""Web 服务：FastAPI + 后台采集引擎 + 静态单页仪表盘。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import Auth
from .engine import Engine

logger = logging.getLogger("webapp")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.environ.get("CX_CONFIG", os.path.join(os.path.dirname(BASE_DIR), "config.yaml"))

ENGINE: Optional[Engine] = None
AUTH: Optional[Auth] = None


# ------------------------------------------------------------------ 模型


class LoginPayload(BaseModel):
    password: str = ""


class IgnorePayload(BaseModel):
    key: str
    title: Optional[str] = None
    course_name: Optional[str] = None
    kind: Optional[str] = None
    deadline: Optional[str] = None


class KeyPayload(BaseModel):
    key: str


class SettingsPayload(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    cookie: Optional[str] = None
    interval_seconds: Optional[int] = None
    thresholds_hours: Optional[List[float]] = None
    daily_summary_at: Optional[str] = None
    wecom_webhook: Optional[str] = None
    serverchan_key: Optional[str] = None
    yuketang_cookie: Optional[str] = None
    yuketang_enabled: Optional[bool] = None
    dry_run: Optional[bool] = None
    new_password: Optional[str] = None


# ------------------------------------------------------------------ 生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ENGINE, AUTH
    ENGINE = Engine(CONFIG_PATH)
    AUTH = Auth(ENGINE.data_dir)
    ENGINE.start_scheduler()
    logger.info("服务启动，配置 %s，数据目录 %s", CONFIG_PATH, ENGINE.data_dir)
    try:
        yield
    finally:
        if ENGINE:
            ENGINE.stop_scheduler()
        logger.info("服务停止")


app = FastAPI(title="学习通待办中心", docs_url=None, redoc_url=None, lifespan=lifespan)


def engine() -> Engine:
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return ENGINE


def auth() -> Auth:
    if AUTH is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return AUTH


def auth_required(request: Request) -> None:
    auth().require(request)


# ------------------------------------------------------------------ 页面


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "chaoxing-reminder-web", "version": "1.0.0"}


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


# ------------------------------------------------------------------ 鉴权


@app.get("/api/session")
def session(request: Request) -> Dict[str, Any]:
    token = request.cookies.get("cx_session", "")
    return {"authenticated": bool(token and auth().verify(token))}


@app.post("/api/login")
def login(payload: LoginPayload, request: Request, response: Response) -> Dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    auth().guard(ip)
    if not auth().check_password(payload.password):
        auth().note_fail(ip)
        raise HTTPException(status_code=401, detail="口令错误")
    auth().note_success(ip)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    token = auth().issue()
    auth().set_cookie(response, token, secure=(proto == "https"))
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> Dict[str, Any]:
    auth().clear_cookie(response)
    return {"ok": True}


# ------------------------------------------------------------------ 数据


@app.get("/api/overview")
def overview(_: None = Depends(auth_required)) -> Dict[str, Any]:
    return engine().overview()


@app.post("/api/refresh")
def refresh(_: None = Depends(auth_required)) -> Dict[str, Any]:
    started = engine().trigger_async()
    return {"started": started, "message": "已开始检测" if started else "已有检测在进行中"}


@app.get("/api/events")
def events(limit: int = 50, _: None = Depends(auth_required)) -> Dict[str, Any]:
    return {"items": engine().read_events(max(1, min(limit, 200)))}


@app.get("/api/task/{key}")
def task_detail(key: str, refresh: int = 0, _: None = Depends(auth_required)) -> Dict[str, Any]:
    try:
        return engine().task_detail(key, refresh=bool(refresh))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc


@app.post("/api/ignore")
def ignore(payload: IgnorePayload, _: None = Depends(auth_required)) -> Dict[str, Any]:
    item = engine().ignore_task(payload.key, payload.model_dump())
    return {"ok": True, "item": item}


@app.post("/api/unignore")
def unignore(payload: KeyPayload, _: None = Depends(auth_required)) -> Dict[str, Any]:
    ok = engine().unignore_task(payload.key)
    return {"ok": ok}


# ------------------------------------------------------------------ 雨课堂扫码登录
# 登录态放在进程内存里（服务单 worker），前端轮询 status 驱动流程，
# 不依赖后台常驻线程，二维码过期可随时重开。

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


@app.post("/api/yt/login/start")
def yt_login_start(_: None = Depends(auth_required)) -> Dict[str, Any]:
    import re as _re
    import time as _time
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
        m = _re.search(r"/connect/qrcode/([A-Za-z0-9_\-]+)", page)
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
        "session": session,
        "uuid": qr_uuid,
        "state": state,
        "redirect": redirect,
        "png": png,
        "created": _time.time(),
        "status": "waiting",
        "user": "",
    }
    # 只保留最近 5 个会话
    for key in sorted(_YT_LOGIN, key=lambda k: _YT_LOGIN[k]["created"])[:-5]:
        _YT_LOGIN.pop(key, None)
    return {"id": sid, "expires_in": 300}


@app.get("/api/yt/login/qr")
def yt_login_qr(id: str, _: None = Depends(auth_required)) -> Response:
    item = _YT_LOGIN.get(id)
    if not item:
        raise HTTPException(status_code=404, detail="二维码已失效，请重新获取")
    return Response(content=item["png"], media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/yt/login/status")
def yt_login_status(id: str, _: None = Depends(auth_required)) -> Dict[str, Any]:
    import json as _json
    import re as _re
    import time as _time

    item = _YT_LOGIN.get(id)
    if not item:
        return {"status": "expired", "detail": "会话不存在，请重新获取二维码"}

    if item["status"] == "ok":
        return {"status": "ok", "user": item.get("user", "")}

    if _time.time() - item["created"] > 290:
        item["status"] = "expired"
        return {"status": "expired", "detail": "二维码已过期"}

    session = item["session"]
    try:
        resp = session.get(
            "https://lp.open.weixin.qq.com/connect/l/qrconnect",
            params={"uuid": item["uuid"], "_": int(_time.time() * 1000)},
            timeout=10,
            headers={"Referer": "https://open.weixin.qq.com/"},
        )
        text = resp.text
    except Exception:  # noqa: BLE001
        return {"status": "waiting", "detail": "等待扫码"}

    m_code = _re.search(r"wx_code='([^']*)'", text)
    code = m_code.group(1) if m_code else ""
    if not code:
        errcode = _re.search(r"wx_errcode=(\d+)", text)
        if errcode and errcode.group(1) == "405":
            item["status"] = "expired"
            return {"status": "expired", "detail": "二维码已过期"}
        if errcode and errcode.group(1) in ("404", "403"):
            return {"status": "scanned", "detail": "已扫码，请在手机上确认"}
        return {"status": "waiting", "detail": "等待扫码"}

    # 已确认：换登录态
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

    # 写入配置
    raw = _read_config()
    raw.setdefault("yuketang", {})
    raw["yuketang"]["enabled"] = True
    raw["yuketang"]["cookie"] = cookie_str
    raw["yuketang"]["base_url"] = _YT_BASE
    _write_config(raw)
    engine().reload()

    name = str((info.get("data") or {}).get("name") or "")
    item["status"] = "ok"
    item["user"] = name
    logger.info("雨课堂扫码登录成功：%s", name)
    return {"status": "ok", "user": name}


@app.get("/api/logs")
def logs(lines: int = 120, _: None = Depends(auth_required)) -> Dict[str, Any]:
    eng = engine()
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


def _read_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _write_config(raw: Dict[str, Any]) -> None:
    tmp = f"{CONFIG_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


@app.get("/api/settings")
def get_settings(_: None = Depends(auth_required)) -> Dict[str, Any]:
    eng = engine()
    raw = _read_config()
    channels = raw.get("notify", {}).get("channels") or []
    wecom = next((c.get("webhook", "") for c in channels if c.get("type") == "wecom"), "")
    serverchan = next((c.get("send_key", "") for c in channels if c.get("type") == "serverchan"), "")
    username = raw.get("account", {}).get("username", "") or ""
    masked = (username[:3] + "***" + username[-2:]) if len(username) >= 6 else username
    return {
        "username": username,
        "username_masked": masked,
        "has_password": bool(raw.get("account", {}).get("password")),
        "has_cookie": bool(raw.get("account", {}).get("cookie")),
        "interval_seconds": eng.cfg.poll.interval_seconds,
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


@app.post("/api/settings")
def update_settings(payload: SettingsPayload, _: None = Depends(auth_required)) -> Dict[str, Any]:
    raw = _read_config()
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

    _write_config(raw)

    if payload.new_password:
        try:
            auth().set_password(payload.new_password)
            auth().persist()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    engine().reload()
    return {"ok": True}


@app.post("/api/diagnose")
def diagnose(_: None = Depends(auth_required)) -> Dict[str, Any]:
    """分步诊断：学习通（账号/登录/课程/抽样） + 雨课堂（Cookie/课程/章节叶子）。"""
    import time as _time
    from collections import Counter

    from cxreminder.client import ChaoxingClient
    from cxreminder.utils import now_in
    from cxreminder.yuketang import YuketangClient, _pick, _walk_leaves

    eng = engine()
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
        add("账号配置", False, "既没有配置学习通账号，也没有配置雨课堂 Cookie")
        return {"ok": False, "steps": steps}

    # ------------------------------------------------ 学习通
    if not cx_ready:
        add("学习通", True, "未配置，已跳过")
    else:
        masked = cfg.account.username
        if len(masked) >= 7:
            masked = masked[:3] + "****" + masked[-2:]
        add("学习通账号", True, masked or "Cookie 登录")
        client = ChaoxingClient(cfg)
        t0 = _time.time()
        try:
            client.login()
            add("学习通登录", True, "登录态有效", _time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            add("学习通登录", False, str(exc)[:300], _time.time() - t0)
            client = None
        if client is not None:
            t0 = _time.time()
            try:
                courses = client.fetch_courses()
                add("学习通课程", True, f"{len(courses)} 门课程", _time.time() - t0)
                now = now_in(cfg.timezone)
                for course in courses[:2]:
                    t0 = _time.time()
                    try:
                        tasks = client.fetch_course_tasks(course)
                    except Exception as exc:  # noqa: BLE001
                        add(f"学习通《{course.name[:12]}》", False, str(exc)[:200],
                            _time.time() - t0)
                        continue
                    pending = [t for t in tasks if t.is_pending(cfg.remind, now)]
                    detail = f"共 {len(tasks)} 条，待办 {len(pending)} 条"
                    if tasks:
                        detail += "；示例：" + tasks[0].title[:20]
                    add(f"学习通《{course.name[:12]}》", True, detail, _time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                add("学习通课程", False, str(exc)[:300], _time.time() - t0)

    # ------------------------------------------------ 雨课堂
    if not yt_ready:
        add("雨课堂", True, "未配置，已跳过")
    else:
        yt = YuketangClient(cfg)
        add("雨课堂配置", True, f"Cookie 尾号 …{cfg.yuketang.cookie[-6:]}")
        t0 = _time.time()
        try:
            info = yt.login()
            name = _pick(info, "name", "nickname", "school_number", default="已登录")
            add("雨课堂登录", True, str(name)[:40], _time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            add("雨课堂登录", False, str(exc)[:300], _time.time() - t0)
            yt = None
        if yt is not None:
            t0 = _time.time()
            try:
                courses = yt.fetch_courses()
                add("雨课堂课程", True, f"{len(courses)} 门课程", _time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                add("雨课堂课程", False, str(exc)[:300], _time.time() - t0)
                courses = []

            for course in courses[:2]:
                t0 = _time.time()
                try:
                    tasks = yt.fetch_course_tasks(course)
                except Exception as exc:  # noqa: BLE001
                    add(f"雨课堂《{course.label[:12]}》", False, str(exc)[:200],
                        _time.time() - t0)
                    continue
                kinds = Counter(t.kind for t in tasks)
                detail = (f"共 {len(tasks)} 条（作业 {kinds.get('homework', 0)} / "
                          f"考试 {kinds.get('exam', 0)}）")
                if tasks:
                    detail += "；示例：" + tasks[0].title[:20]
                else:
                    try:
                        raw = yt.request(
                            "GET", "/mooc-api/v1/lms/learn/course/chapter",
                            params={"cid": course.cid, "classroom_id": course.classroom_id},
                        )
                        leaves = _walk_leaves((raw.get("data") or {}) if isinstance(raw, dict) else {})
                        types = Counter(str(_pick(l, "leaf_type", "leafType", "type")) for l in leaves)
                        detail += f"；章节叶子 {len(leaves)} 个，类型分布 {dict(types)}"
                    except Exception:  # noqa: BLE001
                        pass
                add(f"雨课堂《{course.label[:12]}》", True, detail, _time.time() - t0)

    add("结论", ok_all,
        "连接正常" if ok_all else "存在异常，详见上方步骤")
    return {"ok": ok_all, "steps": steps}


@app.post("/api/test-notify")
def test_notify(_: None = Depends(auth_required)) -> Dict[str, Any]:
    from cxreminder.notify import Notifier

    eng = engine()
    notifier = Notifier(eng.cfg.channels, dry_run=eng.cfg.dry_run, timeout=eng.cfg.request_timeout)
    results = notifier.send(
        "学习通提醒：通道测试",
        f"### 这是一条测试消息\n\n- 时间：{eng.overview()['now']}\n- 收到即表示通道配置正确。",
    )
    return {
        "results": [{"channel": n, "ok": ok, "detail": detail} for n, ok, detail in results]
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
