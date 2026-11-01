"""多渠道提醒推送。任一通道失败不影响其它通道，全部异常都会被记录。"""
from __future__ import annotations

import json
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


class ChannelError(RuntimeError):
    pass


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> str:
    import requests

    resp = requests.post(url, json=payload, timeout=timeout)
    if resp.status_code >= 300:
        raise ChannelError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.text[:500]


class Notifier:
    def __init__(self, channels: List[Dict[str, Any]], dry_run: bool = False,
                 timeout: float = 10.0):
        self.channels = channels
        self.dry_run = dry_run
        self.timeout = timeout

    # ------------------------------------------------------------ 对外接口

    def send(self, title: str, markdown: str) -> List[Tuple[str, bool, str]]:
        """按顺序推送，返回 [(通道名, 是否成功, 说明)]。"""
        results: List[Tuple[str, bool, str]] = []
        for channel in self.channels:
            name = str(channel.get("type", "")).strip().lower()
            if not name:
                continue
            if self.dry_run and name != "console":
                results.append((name, True, "dry-run 跳过实际发送"))
                continue
            try:
                detail = self._dispatch(name, channel, title, markdown)
                results.append((name, True, detail or "ok"))
                logger.info("提醒已发送 [%s]", name)
            except Exception as exc:  # noqa: BLE001 - 单通道失败需继续
                results.append((name, False, str(exc)[:300]))
                logger.error("提醒发送失败 [%s]：%s", name, exc)
        return results

    # ------------------------------------------------------------ 分发

    def _dispatch(self, name: str, cfg: Dict[str, Any], title: str, body: str) -> str:
        handler = getattr(self, f"_send_{name}", None)
        if handler is None:
            raise ChannelError(f"未知的提醒通道类型：{name}")
        return handler(cfg, title, body)

    # ------------------------------------------------------------ 各通道实现

    def _send_console(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        print("\n" + "=" * 64)
        print(title)
        print("-" * 64)
        print(body)
        print("=" * 64 + "\n")
        return "已打印到控制台"

    def _send_serverchan(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        """Server 酱（微信推送）。send_key 形如 SCTxxxxx。"""
        key = cfg.get("send_key") or cfg.get("key")
        if not key:
            raise ChannelError("serverchan 缺少 send_key")
        url = f"https://sctapi.ftqq.com/{key}.send"
        import requests

        resp = requests.post(
            url, data={"title": title[:100], "desp": body}, timeout=self.timeout
        )
        data = resp.json() if resp.text.strip().startswith("{") else {}
        if data.get("code") not in (0, "0", None):
            raise ChannelError(f"Server酱返回错误：{data}")
        return "Server酱已受理"

    def _send_pushplus(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        token = cfg.get("token") or cfg.get("send_key")
        if not token:
            raise ChannelError("pushplus 缺少 token")
        payload = {
            "token": token,
            "title": title[:100],
            "content": body,
            "template": "markdown",
        }
        if cfg.get("topic"):
            payload["topic"] = cfg["topic"]
        text = _post_json("https://www.pushplus.plus/send", payload, self.timeout)
        try:
            data = json.loads(text)
            if data.get("code") not in (200, "200"):
                raise ChannelError(f"PushPlus 返回错误：{data.get('msg') or text}")
        except json.JSONDecodeError:
            pass
        return "PushPlus 已受理"

    def _send_wecom(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        """企业微信群机器人。"""
        url = cfg.get("webhook") or cfg.get("url")
        if not url:
            raise ChannelError("wecom 缺少 webhook 地址")
        payload = {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{body}"}}
        text = _post_json(url, payload, self.timeout)
        if '"errcode":0' not in text.replace(" ", ""):
            raise ChannelError(f"企业微信返回：{text}")
        return "企业微信已受理"

    def _send_dingtalk(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        url = cfg.get("webhook") or cfg.get("url")
        if not url:
            raise ChannelError("dingtalk 缺少 webhook 地址")
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n{body}"}}
        text = _post_json(url, payload, self.timeout)
        if '"errcode":0' not in text.replace(" ", ""):
            raise ChannelError(f"钉钉返回：{text}")
        return "钉钉已受理"

    def _send_webhook(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        """通用 Webhook：POST JSON {title, content, markdown}。"""
        url = cfg.get("url") or cfg.get("webhook")
        if not url:
            raise ChannelError("webhook 缺少 url")
        payload = {
            "title": title,
            "content": body,
            "text": f"{title}\n{body}",
            "markdown": f"## {title}\n{body}",
        }
        headers = cfg.get("headers") or {}
        import requests

        resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        if resp.status_code >= 300:
            raise ChannelError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return f"Webhook 返回 {resp.status_code}"

    def _send_email(self, cfg: Dict[str, Any], title: str, body: str) -> str:
        host = cfg.get("smtp_host")
        port = int(cfg.get("smtp_port") or 465)
        user = cfg.get("username")
        password = cfg.get("password")
        to_addrs = cfg.get("to") or []
        if isinstance(to_addrs, str):
            to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]
        if not (host and user and password and to_addrs):
            raise ChannelError("email 通道需要 smtp_host/username/password/to")

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = user
        msg["To"] = ", ".join(to_addrs)
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=self.timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=self.timeout)
            try:
                server.starttls()
            except smtplib.SMTPException:
                pass
        try:
            server.login(user, password)
            server.sendmail(user, to_addrs, msg.as_string())
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
        return f"邮件已发送至 {len(to_addrs)} 位收件人"
