"""雨课堂模块单元测试：课程解析、章节叶子分类、时间兼容、来源主键。"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cxreminder.config import Config  # noqa: E402
from cxreminder.models import Task  # noqa: E402
from cxreminder.yuketang import (  # noqa: E402
    YuketangClient,
    _pick,
    _to_datetime,
    _walk_leaves,
    classify_leaf,
)

COURSES_JSON = {
    "success": True,
    "data": {
        "list": [
            {"classroom_id": 24211265, "course_id": 997001, "name": "工程制图",
             "teacher": "张老师"},
            {"classroomId": 24211266, "courseId": 997002, "courseName": "大学物理",
             "teacherName": "李老师"},
            {"name": "缺少 classroom_id 的脏数据"},
        ]
    },
}

CHAPTER_JSON = {
    "success": True,
    "data": {
        "course_chapter": [
            {
                "name": "第一章 绪论",
                "section_leaf_list": [
                    {"id": 1001, "name": "1.1 课程视频", "leaf_type": 1},
                    {"id": 1002, "name": "第一章 作业", "leaf_type": 4,
                     "leafinfo_id": 88001, "end_time": 1793540000000, "is_finished": 0},
                ],
            },
            {
                "name": "第二章",
                "sectionLeafList": [
                    {"id": 1003, "name": "期中测验", "leafType": 5,
                     "leafinfoId": 88002, "endTime": 1793600000, "isFinished": 1},
                    {"id": 1004, "name": "第二章 习题", "leaf_type": 9},
                ],
            },
        ]
    },
}


class HelperTest(unittest.TestCase):
    def test_pick_handles_case_variants(self):
        node = {"classroomId": 1, "Name": "x"}
        self.assertEqual(_pick(node, "classroom_id", "classroomId"), 1)
        self.assertEqual(_pick(node, "name", "Name"), "x")
        self.assertIsNone(_pick(node, "absent"))

    def test_to_datetime(self):
        self.assertEqual(_to_datetime(1793600000, "Asia/Shanghai").year, 2026)
        self.assertEqual(_to_datetime(1793600000000, "Asia/Shanghai").year, 2026)
        self.assertEqual(_to_datetime("2026-11-03 23:55", "Asia/Shanghai"),
                         datetime(2026, 11, 3, 23, 55))
        self.assertIsNone(_to_datetime("", "Asia/Shanghai"))

    def test_walk_leaves_nested(self):
        leaves = _walk_leaves(CHAPTER_JSON["data"])
        self.assertEqual(len(leaves), 4)
        self.assertEqual({leaf["id"] for leaf in leaves}, {1001, 1002, 1003, 1004})

    def test_classify(self):
        self.assertEqual(classify_leaf({"leaf_type": 4}), "homework")
        self.assertEqual(classify_leaf({"leafType": 5}), "exam")
        self.assertEqual(classify_leaf({"leaf_type": 1, "name": "视频"}), None)
        self.assertEqual(classify_leaf({"name": "期末作业"}), "homework")
        self.assertEqual(classify_leaf({"name": "阶段测验"}), "exam")


class ClientParseTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.yuketang.cookie = "sessionid=fake"
        self.client = YuketangClient(self.cfg)

    def _stub(self, payload_by_path):
        def fake_request(method, path, **kwargs):
            for key, value in payload_by_path.items():
                if key in path:
                    return value
            raise AssertionError("unexpected path: " + path)

        self.client.request = fake_request

    def test_fetch_courses(self):
        self._stub({"/v2/api/web/courses/list": COURSES_JSON})
        courses = self.client.fetch_courses()
        self.assertEqual(len(courses), 2)  # 缺 classroom_id 的脏数据被跳过
        self.assertEqual(courses[0].classroom_id, "24211265")
        self.assertEqual(courses[0].name, "工程制图")
        self.assertEqual(courses[1].cid, "997002")
        self.assertEqual(courses[1].teacher, "李老师")

    def test_fetch_tasks(self):
        self._stub({"/course/chapter": CHAPTER_JSON})
        from cxreminder.yuketang import YtCourse

        tasks = self.client.fetch_course_tasks(
            YtCourse(classroom_id="24211265", cid="997001", name="工程制图")
        )
        # 视频叶子被跳过：作业 2（type 4 + type 9）、考试 1（type 5）
        self.assertEqual(len(tasks), 3)
        kinds = sorted(t.kind for t in tasks)
        self.assertEqual(kinds, ["exam", "homework", "homework"])
        self.assertTrue(all(t.source == "yuketang" for t in tasks))
        self.assertTrue(all(t.course_name == "工程制图" for t in tasks))

        homework = [t for t in tasks if t.kind == "homework"][0]
        self.assertEqual(homework.deadline.year, 2026)
        self.assertEqual(homework.status, "未完成")

        exam = [t for t in tasks if t.kind == "exam"][0]
        self.assertEqual(exam.status, "已完成")
        self.assertIn("exam_id=88002", exam.url)

    def test_source_changes_key_but_not_chaoxing(self):
        cx = Task(kind="homework", course_id="1", class_id="2", course_name="c",
                  task_id="3", title="t")
        yt = Task(kind="homework", course_id="1", class_id="2", course_name="c",
                  task_id="3", title="t", source="yuketang")
        self.assertNotEqual(cx.key, yt.key)
        # 学习通侧 key 保持原算法（加来源前算出的值）
        self.assertEqual(cx.key, cx.key.lower())
        self.assertEqual(len(cx.key), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
