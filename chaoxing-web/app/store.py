"""用户与会话存储（SQLite，标准库）。

多用户场景下不再依赖单个口令文件，改为：

    users(id, username, display_name, password_hash, created_at, last_login_at, status)
    sessions(token_hash, user_id, created_at, expires_at, user_agent)

会话令牌只在 Cookie 里出现，库里存 sha256 指纹；过期由 `resolve_session` 判断。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .security import hash_password, token_fingerprint, verify_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    status        TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class UserStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 用户

    def create_user(self, username: str, password: str,
                    display_name: str = "") -> Dict[str, Any]:
        username = (username or "").strip()
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, display_name, password_hash, created_at)"
                " VALUES(?,?,?,?)",
                (username, display_name or username, hash_password(password), now),
            )
            user_id = int(cur.lastrowid)
        return self.get(user_id) or {}

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=?", ((username or "").strip(),)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.get_by_username(username)
        if not user or user.get("status") != "active":
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        self.touch_login(user["id"])
        return self.get(user["id"])

    def touch_login(self, user_id: int) -> None:
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, user_id))

    def set_password(self, user_id: int, new_password: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(new_password), user_id),
            )
        self.revoke_all(user_id)

    def verify_user_password(self, user_id: int, password: str) -> bool:
        user = self.get(user_id)
        return bool(user and verify_password(password, user["password_hash"]))

    # ------------------------------------------------------------ 会话

    def create_session(self, user_id: int, token: str,
                       days: int = 30, user_agent: str = "") -> str:
        now = datetime.now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent)"
                " VALUES(?,?,?,?,?)",
                (
                    token_fingerprint(token),
                    user_id,
                    now.isoformat(sep=" ", timespec="seconds"),
                    (now + timedelta(days=days)).isoformat(sep=" ", timespec="seconds"),
                    (user_agent or "")[:200],
                ),
            )
        return token

    def resolve_session(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        fp = token_fingerprint(token)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (fp,)).fetchone()
            if not row:
                return None
            try:
                expires = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                expires = datetime.min
            if expires < datetime.now():
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (fp,))
                return None
            user = conn.execute(
                "SELECT * FROM users WHERE id=? AND status='active'", (row["user_id"],)
            ).fetchone()
        return dict(user) if user else None

    def revoke_session(self, token: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_fingerprint(token),))

    def revoke_all(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def purge_expired(self) -> int:
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return cur.rowcount or 0
