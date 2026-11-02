"""两端登录状态判定（前端弹窗提醒的依据）。"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(os.path.dirname(BASE), "chaoxing-reminder"))

import yaml  # noqa: E402

from app.engine import Engine  # noqa: E402


class SourceAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-auth-")
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        self.cfg = {
            "account": {"username": "", "password": "", "cookie": ""},
            "yuketang": {"enabled": False, "cookie": ""},
            "poll": {"interval_seconds": 900},
            "storage": {"data_dir": os.path.join(self.tmp, "data")},
            "notify": {"channels": [{"type": "console"}]},
        }
        self._flush()

    def _flush(self):
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.cfg, fh, allow_unicode=True, sort_keys=False)

    def _engine(self, account=None, yuketang=None):
        if account:
            self.cfg["account"].update(account)
        if yuketang:
            self.cfg["yuketang"].update(yuketang)
        self._flush()
        return Engine(self.cfg_path)

    def test_unbound_by_default(self):
        auth = self._engine().source_auth()
        self.assertEqual(auth["chaoxing"]["state"], "unbound")
        self.assertEqual(auth["yuketang"]["state"], "unbound")
        self.assertEqual(auth["chaoxing"]["label"], "学习通")

    def test_bound_but_never_run(self):
        eng = self._engine(account={"username": "13800000000", "password": "x"})
        self.assertEqual(eng.source_auth()["chaoxing"]["state"], "unknown")

    def test_expired_on_auth_error(self):
        eng = self._engine(
            account={"username": "13800000000", "password": "x"},
            yuketang={"enabled": True, "cookie": "sessionid=abc"},
        )
        eng.last_run = datetime.now()
        eng.last_summary = {"errors": [
            "登录/鉴权失败：账号或密码错误",
            "雨课堂登录失败：Cookie 已失效，请重新扫码",
        ]}
        auth = eng.source_auth()
        self.assertEqual(auth["chaoxing"]["state"], "expired")
        self.assertEqual(auth["yuketang"]["state"], "expired")
        self.assertIn("Cookie 已失效", auth["yuketang"]["detail"])

    def test_ok_after_successful_run(self):
        eng = self._engine(
            account={"username": "13800000000", "password": "x"},
            yuketang={"enabled": True, "cookie": "sessionid=abc"},
        )
        eng.last_run = datetime.now()
        eng.last_summary = {"errors": []}
        auth = eng.source_auth()
        self.assertEqual(auth["chaoxing"]["state"], "ok")
        self.assertEqual(auth["yuketang"]["state"], "ok")

    def test_other_errors_do_not_mark_expired(self):
        """网络抖动不算登录失效，避免误弹窗。"""
        eng = self._engine(account={"username": "13800000000", "password": "x"})
        eng.last_run = datetime.now()
        eng.last_summary = {"errors": ["网络或接口异常：ReadTimeout"]}
        self.assertEqual(eng.source_auth()["chaoxing"]["state"], "ok")

    def test_overview_carries_auth(self):
        eng = self._engine()
        data = eng.overview()
        self.assertIn("auth", data)
        self.assertEqual(set(data["auth"]), {"chaoxing", "yuketang"})


if __name__ == "__main__":
    unittest.main()
