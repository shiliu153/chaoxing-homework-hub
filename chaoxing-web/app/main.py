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
    """分步诊断：账号配置 -> 登录 -> 课程列表 -> 抽样课程的作业/考试。"""
    import time as _time

    from cxreminder.client import ChaoxingClient
    from cxreminder.utils import now_in

    eng = engine()
    cfg = eng.cfg
    steps: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, cost: float = 0.0) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail,
                      "cost": round(cost, 1)})

    if not (cfg.account.cookie or (cfg.account.username and cfg.account.password)):
        add("账号配置", False, "尚未填写学习通账号或 Cookie")
        return {"ok": False, "steps": steps}

    masked = cfg.account.username
    if len(masked) >= 7:
        masked = masked[:3] + "****" + masked[-2:]
    add("账号配置", True, f"{masked or 'Cookie 登录'}")

    client = ChaoxingClient(cfg)
    t0 = _time.time()
    try:
        client.login()
        add("登录学习通", True, "登录态有效" if not cfg.account.cookie else "Cookie 有效",
            _time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        add("登录学习通", False, str(exc)[:300], _time.time() - t0)
        return {"ok": False, "steps": steps}

    t0 = _time.time()
    try:
        courses = client.fetch_courses()
        add("获取课程列表", True, f"{len(courses)} 门课程", _time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        add("获取课程列表", False, str(exc)[:300], _time.time() - t0)
        return {"ok": False, "steps": steps}

    now = now_in(cfg.timezone)
    total_tasks = 0
    sample = courses[:2]
    for course in sample:
        t0 = _time.time()
        try:
            tasks = client.fetch_course_tasks(course)
        except Exception as exc:  # noqa: BLE001
            add(f"读取《{course.name[:14]}》", False, str(exc)[:200], _time.time() - t0)
            continue
        total_tasks += len(tasks)
        pending = [t for t in tasks if t.is_pending(cfg.remind, now)]
        detail = f"共 {len(tasks)} 条，待办 {len(pending)} 条"
        if tasks:
            detail += "；示例：" + tasks[0].title[:24]
        add(f"读取《{course.name[:14]}》", True, detail, _time.time() - t0)

    add("结论", True,
        f"连接正常。已抽检 {len(sample)} 门课共 {total_tasks} 条记录；"
        f"完整数据请点「立即检测」全量刷新。")
    return {"ok": True, "steps": steps, "courses": len(courses)}


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
