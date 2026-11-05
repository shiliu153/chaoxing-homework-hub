"""解析层单元测试：python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cxreminder.models import KIND_EXAM, KIND_HOMEWORK, Course  # noqa: E402
from cxreminder.parsers import (  # noqa: E402
    parse_exam_list_html,
    parse_work_detail,
    parse_course_list_html,
    parse_course_list_json,
    parse_deadline,
    parse_task_list_html,
    pick_title,
)

NOW = datetime(2026, 11, 1, 20, 0)

WORK_PAGE = """
<html><body>
<div class="ulDiv">
  <ul>
    <li>
      <div class="tit"><a href="javascript:;" onclick="toDetail('2345678')"
         title="第三章 导数与微分 课后作业">第三章 导数与微分 课后作业</a></div>
      <div class="status">未交</div>
      <div class="time">截止时间：2026-11-02 23:59</div>
    </li>
    <li>
      <div class="tit"><a title="线性代数 第二次作业">线性代数 第二次作业</a></div>
      <span class="status">已交</span>
      <span>截止时间：2026-10-30 23:59</span>
    </li>
    <li>
      <div class="tit"><a title="概率论 单元测验">概率论 单元测验</a></div>
      <span>未交</span>
      <span>剩余时间：2 天 3 小时</span>
    </li>
  </ul>
</div>
</body></html>
"""

EXAM_PAGE = """
<html><body>
<ul>
  <li>
    <a title="大学英语 期中考试" href="/mooc2/exam/preview?examId=9876543">大学英语 期中考试</a>
    <p>开始时间：2026-11-05 14:00</p>
    <p>结束时间：2026-11-05 16:00</p>
    <span>未开始</span>
  </li>
  <li>
    <a title="毛概 期末考试">毛概 期末考试</a>
    <p>开始时间：2026-10-20 09:00</p>
    <p>结束时间：2026-10-20 11:00</p>
    <span>已结束</span>
  </li>
</ul>
</body></html>
"""

COURSE_JSON = """
{"channelList": [
  {"content": {"id": "11223344", "cpi": "556677", "enc": "abcd1234efgh5678",
    "course": {"data": [{"id": "241234567", "name": "高等数学（上）"}]}}},
  {"content": {"id": "11223344", "cpi": "556677",
    "course": {"data": [{"id": "241234567", "name": "高等数学（上）"}]}}},
  {"content": {"id": "99887766", "cpi": "556677",
    "course": {"data": [{"id": "249999999", "name": "大学英语"}]}}}
]}
"""

COURSE_HTML = """
<div class="course-list">
  <div class="course">
    <a href="https://mooc1.chaoxing.com/mooc2-ans/mycourse/stu?courseid=241234567&
       clazzid=11223344&enc=abcd1234efgh5678&cpi=556677" title="高等数学（上）">高等数学（上）</a>
  </div>
  <div class="course">
    <a href="https://mooc1.chaoxing.com/mooc2-ans/mycourse/stu?clazzid=99887766&
       courseid=249999999&cpi=556677" title="大学英语">大学英语</a>
  </div>
</div>
"""


class DeadlineTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(
            parse_deadline("截止时间：2026-11-02 23:59", NOW),
            datetime(2026, 11, 2, 23, 59),
        )

    def test_chinese_format(self):
        self.assertEqual(
            parse_deadline("答题截止 2026年11月5日 16时30分", NOW),
            datetime(2026, 11, 5, 16, 30),
        )

    def test_month_day_infer_year(self):
        self.assertEqual(
            parse_deadline("截止 12-25 23:59", NOW),
            datetime(2026, 12, 25, 23, 59),
        )
        # 已过去的月份视为下一年
        self.assertEqual(
            parse_deadline("截止 03-01 09:00", NOW),
            datetime(2027, 3, 1, 9, 0),
        )

    def test_prefers_deadline_keyword_over_publish_time(self):
        text = "发布时间：2026-10-28 08:00 截止时间：2026-11-08 22:00"
        self.assertEqual(parse_deadline(text, NOW), datetime(2026, 11, 8, 22, 0))

    def test_none_when_absent(self):
        self.assertIsNone(parse_deadline("剩余时间：2 天 3 小时", NOW))
        self.assertIsNone(parse_deadline("", NOW))


DETAIL_PAGE = """
<html><body>
<h2 class="type_tit">一. 简答题（共1题，100分）</h2>
<div class="padBom50 questionLi singleQuesId" typeName="简答题" id="question406044460">
  <h3 class="mark_name colorDeep workTextWrap">1.
    <span class="colorShallow">(简答题)</span>
    <p>在卷积神经网络的基础上，加入 LSTM，共同实现手写字识别</p>
  </h3>
  <div class="stem_answer"><textarea id="answer406044460"></textarea></div>
</div>
<h2 class="type_tit">二. 单选题（共2题，20分）</h2>
<div class="padBom50 questionLi" typeName="单选题" id="question406044461">
  <h3 class="mark_name"><span class="colorShallow">(单选题)</span><p>下列哪个是激活函数？</p></h3>
  <ul><li>A. ReLU</li><li>B. Adam</li></ul>
  <div class="stem_answer"></div>
</div>
<div class="padBom50 questionLi" typeName="单选题" id="question406044462">
  <h3 class="mark_name"><span class="colorShallow">(单选题)</span><p>BatchNorm 的作用是？</p></h3>
  <ul><li>A. 归一化</li><li>B. 剪枝</li></ul>
  <div class="stem_answer"></div>
</div>
</body></html>
"""


class WorkDetailTest(unittest.TestCase):
    def test_questions_groups_and_score(self):
        d = parse_work_detail(DETAIL_PAGE)
        self.assertEqual(len(d["questions"]), 3)
        self.assertEqual(d["total_score"], 120.0)
        first = d["questions"][0]
        self.assertEqual(first["type"], "简答题")
        self.assertEqual(first["score"], 100.0)  # 单题大题继承分值
        self.assertIn("LSTM", first["stem"])
        self.assertNotIn("(简答题)", first["stem"])

        second = d["questions"][1]
        self.assertEqual(second["type"], "单选题")
        self.assertEqual(second["options"], ["A. ReLU", "B. Adam"])
        self.assertEqual(second["score"], 0.0)   # 两题共 20 分，不强行摊分

    def test_empty_page(self):
        d = parse_work_detail("")
        self.assertEqual(d["questions"], [])
        self.assertEqual(d["text"], "")


class CourseTest(unittest.TestCase):
    def test_json(self):
        courses = parse_course_list_json(COURSE_JSON)
        self.assertEqual(len(courses), 2)  # 同一教学班去重
        first = courses[0]
        self.assertEqual(first.course_id, "241234567")
        self.assertEqual(first.class_id, "11223344")
        self.assertEqual(first.name, "高等数学（上）")
        self.assertEqual(first.cpi, "556677")
        self.assertEqual(first.enc, "abcd1234efgh5678")

    def test_html(self):
        courses = parse_course_list_html(COURSE_HTML)
        self.assertEqual(len(courses), 2)
        by_id = {c.course_id: c for c in courses}
        self.assertIn("241234567", by_id)
        self.assertEqual(by_id["241234567"].class_id, "11223344")
        self.assertEqual(by_id["249999999"].class_id, "99887766")


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.course = Course(course_id="241234567", class_id="11223344", name="高等数学（上）")

    def test_homework(self):
        tasks = parse_task_list_html(WORK_PAGE, KIND_HOMEWORK, self.course, NOW)
        self.assertEqual(len(tasks), 3, [t.title for t in tasks])

        first = tasks[0]
        self.assertEqual(first.title, "第三章 导数与微分 课后作业")
        self.assertEqual(first.task_id, "2345678")
        self.assertEqual(first.deadline, datetime(2026, 11, 2, 23, 59))
        self.assertEqual(first.status, "未交")
        self.assertEqual(first.course_name, "高等数学（上）")

        statuses = {t.title: t.status for t in tasks}
        self.assertEqual(statuses["线性代数 第二次作业"], "已交")
        # 无明确时间时保持 None，交给上层决定
        self.assertIsNone(tasks[2].deadline)

    def test_exam_uses_end_time(self):
        tasks = parse_task_list_html(EXAM_PAGE, KIND_EXAM, self.course, NOW)
        self.assertEqual(len(tasks), 2)
        first = tasks[0]
        self.assertEqual(first.title, "大学英语 期中考试")
        self.assertEqual(first.task_id, "9876543")
        self.assertEqual(first.deadline, datetime(2026, 11, 5, 16, 0))
        self.assertEqual(first.status, "未开始")

    def test_task_ids_are_stable_and_distinct(self):
        tasks = parse_task_list_html(WORK_PAGE, KIND_HOMEWORK, self.course, NOW)
        keys = [t.key for t in tasks]
        self.assertEqual(len(keys), len(set(keys)))
        again = parse_task_list_html(WORK_PAGE, KIND_HOMEWORK, self.course, NOW)
        self.assertEqual(keys, [t.key for t in again])

    def test_does_not_pick_course_id_as_task_id(self):
        page = """<ul><li><a href="?courseId=241234567&classId=11223344" title="某作业">
        某作业</a><span>未交</span><span>截止 2026-11-09 12:00</span></li></ul>"""
        tasks = parse_task_list_html(page, KIND_HOMEWORK, self.course, NOW)
        self.assertEqual(len(tasks), 1)
        self.assertNotEqual(tasks[0].task_id, "241234567")

    def test_pick_title_skips_noise(self):
        self.assertEqual(pick_title("<a>查看</a><a>真正的作业标题</a>"), "真正的作业标题")

    def test_empty_page(self):
        self.assertEqual(parse_task_list_html("", KIND_HOMEWORK, self.course, NOW), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


EXAM_LIST_PAGE = """
<div class="bottomList"><ul>
  <li>
    <div style="float:left;width:100%;" onclick="goTest('266525113',11040808,171558311,
      '2026-11-07 23:36:42.0',572383735,false,'fb6134cb39fe93a9977a56ed1055dec4', '0');">
      <div class="tag icon-exam icon-exam"></div>
      <div class="right-content">
        <p class="overHidden2 fl">影视英语与文化-测试5-翻译 2</p>
        <div class="clear"></div>
        <p class="status fl">待做</p>
        <div class="span_tip" title="只允许手机APP考试"><span>只允许手机APP考试</span></div>
      </div>
    </div>
    <div class="time notOver">剩余61小时21分钟</div>
  </li>
  <li>
    <div onclick="viewExamAnswer('10981917','171406590')">
      <p class="overHidden2 fl">影视英语与文化-测试4-作文 1</p>
      <p class="status fl">待批阅</p>
    </div>
    <div class="time">剩余133小时36分钟</div>
  </li>
  <li>
    <div onclick="viewExamAnswer('10941377','171381244')">
      <p class="overHidden2 fl">影视英语与文化-测试3-词汇 2</p>
      <p class="status fl">已完成</p>
    </div>
  </li>
  <li>
    <div onclick="viewExamAnswer('10990000','171999999')">
      <p class="overHidden2 fl">只有剩余时长的测验</p>
      <p class="status fl">待做</p>
    </div>
    <div class="time">剩余2天3小时5分钟</div>
  </li>
</ul></div>
"""


class ExamListTest(unittest.TestCase):
    """考试列表：id 在 onclick 的 goTest/viewExamAnswer 参数里，没有 examId 属性。"""

    def setUp(self):
        self.course = Course("266525113", "153153143", "影视英语与文化")
        self.now = datetime(2026, 11, 5, 10, 15)

    def test_exam_items(self):
        tasks = parse_exam_list_html(
            EXAM_LIST_PAGE, self.course, self.now,
            base_url="https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?x=1",
        )
        self.assertEqual(len(tasks), 4)

        first = tasks[0]
        self.assertEqual(first.kind, "exam")
        self.assertEqual(first.task_id, "11040808")
        self.assertEqual(first.title, "影视英语与文化-测试5-翻译 2")
        self.assertEqual(first.status, "待做")
        self.assertEqual(first.deadline.strftime("%Y-%m-%d %H:%M:%S"), "2026-11-07 23:36:42")
        self.assertEqual(first.extra["relation_id"], "171558311")
        self.assertEqual(first.extra["action"], "goTest")

        self.assertEqual(tasks[1].status, "待批阅")
        self.assertEqual(tasks[1].task_id, "10981917")
        self.assertEqual(tasks[2].status, "已完成")

    def test_relative_remaining_time(self):
        """没有绝对时间时，用「剩余 X 天 Y 小时 Z 分钟」推算。"""
        tasks = parse_exam_list_html(EXAM_LIST_PAGE, self.course, self.now)
        last = tasks[3]
        self.assertEqual(last.title, "只有剩余时长的测验")
        self.assertEqual(last.deadline, self.now + timedelta(days=2, hours=3, minutes=5))

    def test_pending_only_real_exam(self):
        tasks = parse_exam_list_html(EXAM_LIST_PAGE, self.course, self.now)
        remind = type("R", (), {
            "ignore_status_keywords": ["已完成", "待批阅"],
            "pending_status_keywords": ["待做"],
            "only_pending": False,
            "expire_grace_days": 3,
        })()
        pending = [t.title for t in tasks if t.is_pending(remind, self.now)]
        self.assertEqual(pending, ["影视英语与文化-测试5-翻译 2", "只有剩余时长的测验"])

    def test_empty_page(self):
        self.assertEqual(parse_exam_list_html("", self.course, self.now), [])
