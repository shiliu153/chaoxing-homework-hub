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
    YtCourse,
    YuketangClient,
    YuketangError,
    _pick,
    _to_datetime,
    _walk_leaves,
    classify_leaf,
    exam_status_text,
    is_finished_status,
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
        # 课堂章节接口返回空 -> 回退到 mooc 接口
        self._stub({
            "/api/classroom_course_chapter": {"Status": 200, "Success": True,
                                              "course_chapter_tree": []},
            "/course/chapter": CHAPTER_JSON,
        })
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
        self.assertIn("/v2/web/exam/24211265/88002", exam.url)

    def test_classroom_tree_preferred(self):
        """专业版课程：classroom 章节树非空时优先使用，不再走 mooc 接口。"""
        classroom_tree = {"Status": 200, "Success": True, "course_chapter_tree": [
            {"name": "第一章", "section_leaf_list": [
                {"id": 2001, "name": "第一章 作业", "leaf_type": 4,
                 "end_time": 1793600000, "is_finished": 0},
            ]},
        ]}
        self._stub({"/api/classroom_course_chapter": classroom_tree})
        from cxreminder.yuketang import YtCourse

        tasks = self.client.fetch_course_tasks(
            YtCourse(classroom_id="24211265", cid="997001", name="工程制图")
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].kind, "homework")
        self.assertEqual(tasks[0].title, "第一章 作业")

    def test_course_fields_from_nested_object(self):
        """teacher 是对象、course.id 才是课程 ID。"""
        payload = {"data": {"list": [{
            "classroom_id": 24211265,
            "name": "2026秋-工程制图-2班",
            "teacher": {"user_id": 1, "name": "张老师"},
            "course": {"id": 3398735, "name": "工程制图", "type": 2},
        }]}}
        self._stub({"/v2/api/web/courses/list": payload})
        courses = self.client.fetch_courses()
        self.assertEqual(courses[0].classroom_id, "24211265")
        self.assertEqual(courses[0].cid, "3398735")
        self.assertEqual(courses[0].name, "2026秋-工程制图-2班")
        self.assertEqual(courses[0].teacher, "张老师")

    def test_source_changes_key_but_not_chaoxing(self):
        cx = Task(kind="homework", course_id="1", class_id="2", course_name="c",
                  task_id="3", title="t")
        yt = Task(kind="homework", course_id="1", class_id="2", course_name="c",
                  task_id="3", title="t", source="yuketang")
        self.assertNotEqual(cx.key, yt.key)
        # 学习通侧 key 保持原算法（加来源前算出的值）
        self.assertEqual(cx.key, cx.key.lower())
        self.assertEqual(len(cx.key), 16)


EXAM_LIST_JSON = {
    "status": 200,
    "success": True,
    "data": [
        {"id": 2226013, "type": "exam", "title": "第二章 课后作业",
         "classroom_id": 26436138, "classroom_name": "2026秋-现代控制理论-4班"},
        {"id": 2226014, "type": "exam", "title": "期末考试",
         "classroom_id": 26436138, "classroom_name": "2026秋-现代控制理论-4班"},
        {"id": 9001, "type": "quiz", "title": "随堂小测",
         "classroom_id": 26436138, "classroom_name": "2026秋-现代控制理论-4班"},
    ],
}

COVERS = {
    # 已结束（已交）：不应进入待办
    "2226013": {"status": 200, "data": {
        "title": "第二章 课后作业", "start_time": 1793455416000,
        "deadline": 1793631600000, "problem_count": 4, "total_score": 100.0,
        "result": {"status": 4, "unfinished_count": 0, "score": 0.0},
    }},
    # 进行中：应进入待办，截止时间来自 cover
    "2226014": {"status": 200, "data": {
        "title": "期末考试", "start_time": 1793600000000,
        "deadline": 1793631600000, "problem_count": 20, "total_score": 100.0,
        "result": {"status": 1, "unfinished_count": 20},
    }},
}


class ExamCollectTest(unittest.TestCase):
    """考试采集：只收 exam、用 cover 补时间与状态、已交卷的跳过。"""

    def setUp(self):
        self.cfg = Config()
        self.cfg.yuketang.cookie = "sessionid=fake"
        self.client = YuketangClient(self.cfg)

        def fake_request(method, path, **kwargs):
            if "/v2/api/web/courses/list" in path:
                return {"data": {"list": [
                    {"classroom_id": 26436138, "name": "2026秋-现代控制理论-4班",
                     "course": {"id": 1768831, "name": "现代控制理论"}},
                ]}}
            if "/v/rain_exam/get_upcoming_exam" in path:
                return EXAM_LIST_JSON
            if "/v/exam/cover" in path:
                exam_id = str((kwargs.get("params") or {}).get("exam_id"))
                return COVERS.get(exam_id, {"status": 200, "data": {}})
            if "/api/classroom_course_chapter" in path:
                return {"Status": 200, "Success": True, "course_chapter_tree": []}
            if "/mooc-api/v1/lms/learn/course/chapter" in path:
                return {"data": {}}
            if "/api/v3/user/basic-info" in path:
                return {"data": {"name": "陈富"}}
            raise AssertionError("unexpected path: " + path)

        self.client.request = fake_request
        self.client.login = lambda: {"name": "陈富"}

    def test_collect_only_pending_exams(self):
        tasks = self.client.collect()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.kind, "exam")
        self.assertEqual(task.source, "yuketang")
        self.assertEqual(task.task_id, "2226014")
        self.assertEqual(task.title, "期末考试")
        self.assertEqual(task.status, "进行中")
        # 毫秒时间戳换算成 2026 年
        self.assertEqual(task.deadline.year, 2026)
        self.assertEqual(
            task.url,
            "https://changjiang.yuketang.cn/v2/web/exam/26436138/2226014",
        )

    def test_quiz_is_skipped(self):
        tasks = self.client.collect()
        self.assertNotIn("9001", [t.task_id for t in tasks])

    def test_finished_exam_is_skipped(self):
        # 2226013 的 cover 是 result.status=4（已交卷）→ 不进入待办
        self.assertIsNone(
            self.client.build_exam_task("2226013", "26436138", title="第二章 课后作业")
        )

    def test_cover_failure_still_yields_task(self):
        def flaky(method, path, **kwargs):
            if "/v/exam/cover" in path:
                raise YuketangError("boom")
            raise AssertionError(path)

        self.client.request = flaky
        task = self.client.build_exam_task(
            "2226015", "26436138", title="补考", course_name="现代控制理论",
            course=YtCourse(classroom_id="26436138", cid="1768831", name="现代控制理论"),
        )
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "待参加")
        self.assertIn("boom", task.extra.get("cover_error", ""))

    def test_status_mapping(self):
        self.assertEqual(exam_status_text({"status": 0}), "未开始")
        self.assertEqual(exam_status_text({"status": 1}), "进行中")
        self.assertEqual(exam_status_text({}), "待参加")
        self.assertTrue(is_finished_status(exam_status_text({"status": 4})))
        self.assertFalse(is_finished_status(exam_status_text({"status": 1})))

    def test_detail_uses_cover(self):
        task = Task(source="yuketang", kind="exam", course_id="26436138",
                    class_id="1768831", course_name="现代控制理论",
                    task_id="2226014", title="期末考试",
                    extra={"classroom_id": "26436138", "exam_id": "2226014"})
        detail = self.client.fetch_task_detail(task)
        self.assertEqual(detail["question_count"], 20)
        self.assertEqual(detail["total_score"], 100.0)
        self.assertEqual(detail["status"], "进行中")
        self.assertIn("期末", detail["text"])
