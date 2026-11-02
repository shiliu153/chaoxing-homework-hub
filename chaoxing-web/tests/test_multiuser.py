"""多用户改造的核心行为测试：注册、登录、隔离、改密。

不触网：只验证用户/会话/目录/接口鉴权，不跑真实采集。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(os.path.dirname(BASE), "chaoxing-reminder"))

# 环境必须在 import app.main 之前准备好
_TMP = tempfile.mkdtemp(prefix="cx-multi-")
os.environ["CX_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["CX_ALLOW_REGISTRATION"] = "1"
os.environ.pop("CX_INVITE_CODE", None)
os.environ.pop("CX_SERVE_FRONTEND", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


class MultiUserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()   # 触发 lifespan

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        shutil.rmtree(_TMP, ignore_errors=True)

    # ---------------------------------------------------------- 工具

    def _new_client(self) -> TestClient:
        # 不重复触发 lifespan：全局 store/manager 由 setUpClass 启动
        return TestClient(app)

    def _register(self, username: str, password: str = "Passw0rd!") -> TestClient:
        client = self._new_client()
        r = client.post("/api/v1/auth/register",
                        json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return client

    # ---------------------------------------------------------- 用例

    def test_health(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "multi-user")

    def test_anonymous_cannot_read_data(self):
        anon = self._new_client()
        self.assertEqual(anon.get("/api/v1/overview").status_code, 401)
        self.assertEqual(anon.get("/api/v1/settings").status_code, 401)

    def test_register_and_me(self):
        c = self._register("alice_01")
        me = c.get("/api/v1/auth/me").json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"]["username"], "alice_01")
        self.assertFalse(me["account_ready"])

    def test_duplicate_and_weak_password(self):
        self._register("bob_01")
        dup = self.client.post("/api/v1/auth/register",
                               json={"username": "BOB_01", "password": "Passw0rd!"})
        self.assertEqual(dup.status_code, 409)
        weak = self.client.post("/api/v1/auth/register",
                                json={"username": "carol_01", "password": "123"})
        self.assertEqual(weak.status_code, 400)

    def test_settings_are_isolated_per_user(self):
        a = self._register("user_a")
        b = self._register("user_b")

        r = a.post("/api/v1/settings", json={"username": "13800000001",
                                             "password": "secret-a",
                                             "thresholds_hours": [48, 6]})
        self.assertEqual(r.status_code, 200, r.text)

        sa = a.get("/api/v1/settings").json()
        sb = b.get("/api/v1/settings").json()
        self.assertEqual(sa["username"], "13800000001")
        self.assertEqual(sa["thresholds_hours"], [48, 6])
        self.assertEqual(sb["username"], "")
        self.assertNotEqual(sb["thresholds_hours"], [48, 6])

        # 配置目录也必须是分开的
        me_a = a.get("/api/v1/auth/me").json()["user"]["id"]
        me_b = b.get("/api/v1/auth/me").json()["user"]["id"]
        self.assertNotEqual(me_a, me_b)
        cfg_a = os.path.join(os.environ["CX_DATA_DIR"], "users", str(me_a), "config.yaml")
        cfg_b = os.path.join(os.environ["CX_DATA_DIR"], "users", str(me_b), "config.yaml")
        self.assertTrue(os.path.isfile(cfg_a) and os.path.isfile(cfg_b))
        with open(cfg_a, encoding="utf-8") as fh:
            self.assertIn("13800000001", fh.read())
        with open(cfg_b, encoding="utf-8") as fh:
            self.assertNotIn("13800000001", fh.read())

    def test_logout_invalidates_session(self):
        c = self._register("dave_01")
        self.assertEqual(c.get("/api/v1/auth/me").status_code, 200)
        c.post("/api/v1/auth/logout")
        self.assertEqual(c.get("/api/v1/auth/me").status_code, 401)

    def test_change_password_requires_old_and_revokes_sessions(self):
        c = self._register("erin_01", "OldPassw0rd")
        bad = c.post("/api/v1/auth/password",
                     json={"old_password": "wrong", "new_password": "NewPassw0rd"})
        self.assertEqual(bad.status_code, 400)

        ok = c.post("/api/v1/auth/password",
                    json={"old_password": "OldPassw0rd", "new_password": "NewPassw0rd"})
        self.assertEqual(ok.status_code, 200)
        # 旧会话被吊销
        self.assertEqual(c.get("/api/v1/auth/me").status_code, 401)

        fresh = self._new_client()
        login = fresh.post("/api/v1/auth/login",
                           json={"username": "erin_01", "password": "NewPassw0rd"})
        self.assertEqual(login.status_code, 200)
        old = fresh.post("/api/v1/auth/login",
                         json={"username": "erin_01", "password": "OldPassw0rd"})
        self.assertEqual(old.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
