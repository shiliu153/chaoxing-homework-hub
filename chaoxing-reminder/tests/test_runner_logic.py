"""提醒决策逻辑测试：首轮基线、新任务、阈值去重、逾期提醒。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cxreminder.config import Config, RemindConfig  # noqa: E402
from cxreminder.models import KIND_HOMEWORK, Task  # noqa: E402
from cxreminder.runner import EVENT_DUE, EVENT_NEW, EVENT_OVERDUE, Runner  # noqa: E402

NOW = datetime(2026, 11, 1, 20, 0)


def make_task(task_id: str, hours: float | None, title: str = "作业A",
              status: str = "未交") -> Task:
    return Task(
        kind=KIND_HOMEWORK,
        course_id="241234567",
        class_id="11223344",
        course_name="高等数学（上）",
        task_id=task_id,
        title=title,
        deadline=None if hours is None else NOW + timedelta(hours=hours),
        status=status,
    )


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cxreminder-test-")
        self.cfg = Config()
        self.cfg.storage.data_dir = self.tmp
        self.cfg.remind = RemindConfig(thresholds_hours=[72.0, 24.0, 3.0])
        self.runner = Runner(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def diff(self, tasks, bootstrap):
        return self.runner._diff(tasks, NOW, bootstrap)

    def test_bootstrap_is_silent_but_keeps_future_thresholds(self):
        base = self.diff([make_task("1", hours=100)], bootstrap=True)
        self.assertEqual(base, [])
        entry = self.runner.state.get(make_task("1", 100).key)
        self.assertTrue(entry["new_notified"])
        self.assertEqual(entry["thresholds_notified"], [])  # 100h 尚未进入 72h

        # 后续进入 72h 阈值，应提醒一次
        events = self.diff([make_task("1", hours=60)], bootstrap=False)
        self.assertEqual([e.kind for e in events], [EVENT_DUE])
        self.assertEqual(events[0].threshold, 72.0)

        # 同一阈值不重复提醒
        self.assertEqual(self.diff([make_task("1", hours=50)], bootstrap=False), [])

        # 跨到 24h 阈值再提醒一次，且只报最紧的那个
        events = self.diff([make_task("1", hours=10)], bootstrap=False)
        self.assertEqual([e.threshold for e in events], [24.0])

    def test_bootstrap_seeds_crossed_thresholds(self):
        events = self.diff([make_task("2", hours=2)], bootstrap=True)
        self.assertEqual(events, [])
        entry = self.runner.state.get(make_task("2", 2).key)
        self.assertEqual(entry["thresholds_notified"], [3.0, 24.0, 72.0])

    def test_new_task_after_baseline_notifies(self):
        self.diff([make_task("1", hours=100)], bootstrap=True)
        events = self.diff(
            [make_task("1", hours=99), make_task("9", hours=200, title="新布置作业")],
            bootstrap=False,
        )
        self.assertEqual([e.kind for e in events], [EVENT_NEW])
        self.assertEqual(events[0].task.title, "新布置作业")

    def test_overdue_notified_once(self):
        self.diff([make_task("3", hours=5)], bootstrap=False)  # 首次出现即新任务
        events = self.diff([make_task("3", hours=-2)], bootstrap=False)
        self.assertEqual([e.kind for e in events], [EVENT_OVERDUE])
        self.assertEqual(self.diff([make_task("3", hours=-3)], bootstrap=False), [])

    def test_ignore_status_filters_submitted(self):
        submitted = make_task("4", hours=30, status="已交")
        self.assertFalse(submitted.is_pending(self.cfg.remind, NOW))
        pending = make_task("5", hours=30, status="未交")
        self.assertTrue(pending.is_pending(self.cfg.remind, NOW))

    def test_old_deadline_pruned_by_grace(self):
        old = make_task("6", hours=-24 * 30)
        self.assertFalse(old.is_pending(self.cfg.remind, NOW))

    def test_state_persists_across_instances(self):
        self.diff([make_task("7", hours=10)], bootstrap=False)
        self.runner.state.save()

        cfg2 = Config()
        cfg2.storage.data_dir = self.tmp
        runner2 = Runner(cfg2)
        self.assertFalse(runner2.state.is_empty())
        self.assertEqual(runner2._diff([make_task("7", hours=9)], NOW, False), [])


class SummaryTest(unittest.TestCase):
    def test_markdown_and_console(self):
        from cxreminder.runner import Event, Summary

        task = make_task("1", hours=5, title="第三章习题")
        summary = Summary(
            started_at=NOW,
            finished_at=NOW,
            now=NOW,
            all_tasks=[task],
            pending=[task],
            events=[Event(EVENT_DUE, task, threshold=24.0)],
            courses=1,
        )
        md = summary.to_markdown()
        self.assertIn("即将到期", md)
        self.assertIn("第三章习题", md)
        self.assertIn("剩 5 小时", md)
        console = summary.to_console()
        self.assertIn("课程 1", console)


if __name__ == "__main__":
    unittest.main(verbosity=2)
