import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nlp_classroom_intent_router.case import (
    DISP_ISSUED,
    DISP_WITHDRAWN,
    EVENT_CLASSIFICATION,
    EVENT_DISPOSITION_ISSUED,
    EVENT_DISPOSITION_WITHDRAWN,
    EVENT_RECONCILIATION,
    EVENT_TEACHER_DECISION,
)
from nlp_classroom_intent_router.intents import (
    ANSWER_REQUEST,
    CLASSIFIED,
    INTENT_DISPOSITIONS,
    KNOWLEDGE_FOLLOWUP,
    MANUAL_REVIEW,
    PENDING,
    STEP_EXPLANATION,
    STUDENT_HINT,
    TEACHER_REMINDER,
    classify,
    classification_key,
)
from nlp_classroom_intent_router.repository import RecordRepository
from nlp_classroom_intent_router.workflow import TextWorkflow


class StaticClock:
    def __init__(self):
        self.ticks = itertools.count(1)

    def __call__(self):
        return f"2026-11-18T11:{next(self.ticks):03d}:00Z"


def make_record(record_id, text, context, revision=1, source="school-synthetic"):
    return {
        "record_id": record_id,
        "source": source,
        "revision": revision,
        "observed_at": "2026-11-18T09:00:00Z",
        "text": text,
        "context": context,
    }


class ClassifierTests(unittest.TestCase):
    def test_three_intents_classify_to_distinct_dispositions(self):
        knowledge = classify("光合作用和呼吸作用有什么区别？", "生物课")
        self.assertEqual(knowledge["status"], CLASSIFIED)
        self.assertEqual(knowledge["intent"], KNOWLEDGE_FOLLOWUP)
        self.assertTrue(knowledge["classifier_version"])

        steps = classify("这道方程题的解题步骤是什么？", "数学课")
        self.assertEqual(steps["intent"], STEP_EXPLANATION)

        answer = classify("别讲了，直接把这页作业的答案给我吧。", "数学课")
        self.assertEqual(answer["intent"], ANSWER_REQUEST)

        channels = {
            row["intent"] for row in (knowledge, steps, answer)
        }
        self.assertEqual(channels, {KNOWLEDGE_FOLLOWUP, STEP_EXPLANATION, ANSWER_REQUEST})

    def test_missing_context_keeps_request_pending(self):
        result = classify("这个公式怎么用？", "")
        self.assertEqual(result["status"], PENDING)
        self.assertIsNone(result["intent"])
        self.assertIn("上下文", result["reason"])

    def test_conflicting_signals_keep_request_pending(self):
        # 同权重意图各命中一个信号（步骤 vs 答案），无法自动判定
        result = classify("把这道题的答案按步骤给我", "通用语境")
        self.assertEqual(result["status"], PENDING)
        self.assertIsNone(result["intent"])
        self.assertIn("信号冲突", result["reason"])

    def test_classification_key_uses_text_and_context(self):
        key_a = classification_key("同一文本", "课程任务一")
        key_b = classification_key("同一文本", "课程任务二")
        key_c = classification_key("同一文本", "课程任务一")
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(key_a, key_c)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = RecordRepository(Path(self.temp.name) / "records.json")
        self.workflow = TextWorkflow(self.repository, clock=StaticClock())

    def view(self, record_id):
        return self.workflow.review_view(record_id)

    def test_ingest_classifies_and_issues_matching_disposition(self):
        self.workflow.ingest([make_record("r1", "光合作用是什么原理？", "生物课")])
        view = self.view("r1")
        self.assertEqual(view["current"]["status"], CLASSIFIED)
        self.assertEqual(view["current"]["intent"], KNOWLEDGE_FOLLOWUP)
        self.assertEqual(view["current"]["source"], "classifier:rule-based-v1")
        self.assertEqual(len(view["active_dispositions"]), 1)
        self.assertEqual(view["active_dispositions"][0]["type"], STUDENT_HINT)
        self.assertEqual(view["active_dispositions"][0]["status"], DISP_ISSUED)

    def test_answer_request_routes_to_teacher_reminder(self):
        self.workflow.ingest([make_record("r2", "直接把作业答案给我", "数学课作业")])
        view = self.view("r2")
        self.assertEqual(view["current"]["intent"], ANSWER_REQUEST)
        self.assertEqual(view["active_dispositions"][0]["type"], TEACHER_REMINDER)

    def test_pending_record_can_be_completed_after_context_supplied(self):
        self.workflow.ingest([make_record("r3", "这个公式怎么处理？", "")])
        view = self.view("r3")
        self.assertEqual(view["current"]["status"], PENDING)
        self.assertEqual(view["active_dispositions"], [])

        # 先补齐上下文：上下文充分但规则仍无法判定，继续待判
        updated = self.workflow.supplement_context("r3", "高一物理·力学单元")
        self.assertEqual(updated["current"]["status"], PENDING)
        self.assertEqual(updated["context"], "高一物理·力学单元")

        # 再补充带明确步骤信号的上下文，重新分类后发出处置
        updated = self.workflow.supplement_context("r3", "高一物理·力学单元作业，讲解步骤")
        self.assertEqual(updated["current"]["status"], CLASSIFIED)
        self.assertEqual(updated["current"]["intent"], STEP_EXPLANATION)
        self.assertEqual(updated["active_dispositions"][0]["type"], MANUAL_REVIEW)

        timeline_types = [event["event_type"] for event in updated["timeline"]]
        self.assertIn(EVENT_RECONCILIATION, timeline_types)

    def test_replay_does_not_overwrite_teacher_decision_or_disposition(self):
        self.workflow.ingest([make_record("r4", "光合作用是什么？", "生物课")])
        before = self.view("r4")
        self.assertEqual(before["current"]["intent"], KNOWLEDGE_FOLLOWUP)
        hint_id = before["active_dispositions"][0]["disposition_id"]

        # 教师改判为索取答案
        decided = self.workflow.teacher_decide(
            "r4", ANSWER_REQUEST, "teacher-chen", reason="学生实际在讨要作业答案"
        )
        self.assertEqual(decided["current"]["source"], "teacher")
        self.assertEqual(decided["current"]["intent"], ANSWER_REQUEST)
        self.assertEqual(decided["active_dispositions"][0]["type"], TEACHER_REMINDER)

        # 原学生提示被撤回，且保留撤回原因
        history = {item["disposition_id"]: item for item in decided["disposition_history"]}
        withdrawn = history[hint_id]
        self.assertEqual(withdrawn["status"], DISP_WITHDRAWN)
        self.assertIn("教师改判", withdrawn["withdrawn_reason"])

        # 同修订号重放：旧分类标签不得写回
        self.workflow.ingest([make_record("r4", "光合作用是什么？", "生物课")])
        replayed = self.view("r4")
        self.assertEqual(replayed["current"]["intent"], ANSWER_REQUEST)
        self.assertEqual(replayed["current"]["source"], "teacher")
        self.assertEqual([item["type"] for item in replayed["active_dispositions"]],
                         [TEACHER_REMINDER])

        classification_events = [
            event for event in replayed["timeline"]
            if event["event_type"] == EVENT_CLASSIFICATION
        ]
        self.assertTrue(all(event["replayed"] for event in classification_events[1:]))
        reconciliation = [
            event for event in replayed["timeline"]
            if event["event_type"] == EVENT_RECONCILIATION
        ]
        self.assertTrue(any("不回写" in event["reason"] for event in reconciliation))

    def test_teacher_decision_retains_compatible_disposition(self):
        self.workflow.ingest([make_record("r5", "这道题的解题步骤是什么？", "数学课")])
        view = self.view("r5")
        self.assertEqual(view["active_dispositions"][0]["type"], MANUAL_REVIEW)
        review_id = view["active_dispositions"][0]["disposition_id"]

        # 教师改判为同样路由到人工复核的意图……此处构造保留路径：
        # 先改判为知识追问，再改回解释步骤；第二次应保留已有的人工复核处置
        self.workflow.teacher_decide("r5", KNOWLEDGE_FOLLOWUP, "teacher-chen")
        self.workflow.teacher_decide("r5", STEP_EXPLANATION, "teacher-chen")
        final = self.view("r5")
        active = final["active_dispositions"]
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]["disposition_id"], review_id)  # 中间撤回后重新发出
        self.assertEqual(active[0]["type"], MANUAL_REVIEW)

        # 直接保留路径：再次改判为解释步骤（策略通道相同）
        self.workflow.teacher_decide(
            "r5", STEP_EXPLANATION, "teacher-chen", reason="维持原判定"
        )
        final = self.view("r5")
        self.assertEqual(len(final["active_dispositions"]), 1)
        self.assertEqual(final["active_dispositions"][0]["disposition_id"], active[0]["disposition_id"])
        self.assertTrue(
            any("予以保留" in event["reason"] for event in final["timeline"]
                if event["event_type"] == EVENT_RECONCILIATION)
        )

    def test_same_text_in_different_contexts_is_never_deduplicated(self):
        text = "把第三题的思路讲一下"
        self.workflow.ingest([
            make_record("r6a", text, "物理课·随堂测验"),
            make_record("r6b", text, "物理课·课后作业答疑"),
        ])
        view_a = self.view("r6a")
        view_b = self.view("r6b")
        self.assertNotEqual(view_a["classification_key"], view_b["classification_key"])
        self.assertEqual(view_a["text"], view_b["text"])
        self.assertEqual(view_a["current"]["intent"], view_b["current"]["intent"])

        # 改判其中一个，不影响另一个
        self.workflow.teacher_decide("r6a", ANSWER_REQUEST, "teacher-chen")
        self.assertEqual(self.view("r6a")["current"]["intent"], ANSWER_REQUEST)
        self.assertEqual(self.view("r6b")["current"]["intent"], STEP_EXPLANATION)
        self.assertEqual(self.view("r6b")["current"]["source"], "classifier:rule-based-v1")

    def test_context_change_reopens_scope_withdraws_and_reclassifies(self):
        self.workflow.ingest([make_record("r7", "这道题的解题步骤是什么？", "数学课练习", revision=1)])
        first = self.view("r7")
        self.assertEqual(first["active_dispositions"][0]["type"], MANUAL_REVIEW)
        old_key = first["classification_key"]
        old_disp = first["active_dispositions"][0]["disposition_id"]

        # 修订 2：文本相同但课程上下文变化
        self.workflow.ingest([
            make_record("r7", "这道题的解题步骤是什么？", "数学课·考试答疑", revision=2)
        ])
        second = self.view("r7")
        self.assertNotEqual(old_key, second["classification_key"])
        history = {item["disposition_id"]: item for item in second["disposition_history"]}
        self.assertEqual(history[old_disp]["status"], DISP_WITHDRAWN)
        self.assertIn("判题范围", history[old_disp]["withdrawn_reason"])
        self.assertTrue(second["timeline"])
        self.assertEqual(second["current"]["status"], CLASSIFIED)

    def test_stale_revision_is_ignored(self):
        self.workflow.ingest([make_record("r8", "旧稿：原理是什么？", "生物课", revision=2)])
        self.workflow.ingest([make_record("r8", "更旧的稿子", "生物课", revision=1)])
        view = self.view("r8")
        self.assertEqual(view["revision"], 2)
        self.assertIn("旧稿", view["text"])

    def test_invalid_ingest_leaves_state_untouched(self):
        self.workflow.ingest([make_record("r9", "有效文本，原理是什么？", "生物课")])
        invalid = make_record("r10", "缺时间", "生物课")
        invalid["observed_at"] = "not-a-time"
        with self.assertRaises(ValueError):
            self.workflow.ingest([invalid])
        records, cases = self.repository.read_all()
        self.assertEqual([row["record_id"] for row in records], ["r9"])
        self.assertEqual([row["record_id"] for row in cases], ["r9"])

    def test_teacher_decision_validates_inputs(self):
        self.workflow.ingest([make_record("r11", "原理是什么？", "生物课")])
        with self.assertRaises(ValueError):
            self.workflow.teacher_decide("r11", "not-an-intent", "teacher-chen")
        with self.assertRaises(ValueError):
            self.workflow.teacher_decide("r11", ANSWER_REQUEST, "  ")
        with self.assertRaises(KeyError):
            self.workflow.teacher_decide("missing", ANSWER_REQUEST, "teacher-chen")

    def test_audit_reports_consistent_state_and_detects_tampering(self):
        self.workflow.ingest([
            make_record("r12", "光合作用是什么？", "生物课"),
            make_record("r13", "直接给我答案", "数学课作业"),
            make_record("r14", "缺少上下文的请求", ""),
        ])
        self.workflow.teacher_decide("r12", STEP_EXPLANATION, "teacher-chen")
        report = self.workflow.audit()
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["records_checked"], 3)

        # 人为破坏：撤回处置但不记录撤回事件
        records, cases = self.repository.read_all()
        target = next(case for case in cases if case["record_id"] == "r13")
        target["dispositions"][0]["status"] = DISP_WITHDRAWN
        target["dispositions"][0]["withdrawn_event_id"] = None
        self.repository.replace_all(records, cases)
        report = self.workflow.audit()
        self.assertFalse(report["ok"])
        self.assertTrue(any("撤回事件缺失" in issue for issue in report["issues"]))

        # 破坏判题键一致性
        records, cases = self.repository.read_all()
        target = next(case for case in cases if case["record_id"] == "r13")
        target["active_key"] = "deadbeefdeadbeef"
        self.repository.replace_all(records, cases)
        report = self.workflow.audit()
        self.assertTrue(any("判题键" in issue for issue in report["issues"]))

    def test_event_chain_cross_references_decisions_and_dispositions(self):
        self.workflow.ingest([make_record("r15", "解题步骤是什么？", "数学课")])
        self.workflow.teacher_decide("r15", ANSWER_REQUEST, "teacher-chen", reason="改判测试")
        view = self.view("r15")

        event_ids = {event["event_id"] for event in view["timeline"]}
        for item in view["disposition_history"]:
            self.assertIn(item["issued_event_id"], event_ids)
            if item["status"] == DISP_WITHDRAWN:
                self.assertIn(item["withdrawn_event_id"], event_ids)

        teacher_events = [
            event for event in view["timeline"]
            if event["event_type"] == EVENT_TEACHER_DECISION
        ]
        self.assertEqual(len(teacher_events), 1)
        self.assertEqual(teacher_events[0]["teacher"], "teacher-chen")
        self.assertIsNotNone(teacher_events[0]["previous_event_id"])

        issued = [event for event in view["timeline"]
                  if event["event_type"] == EVENT_DISPOSITION_ISSUED]
        self.assertEqual({event["type"] for event in issued},
                         {MANUAL_REVIEW, TEACHER_REMINDER})
        withdrawals = [event for event in view["timeline"]
                       if event["event_type"] == EVENT_DISPOSITION_WITHDRAWN]
        self.assertEqual(len(withdrawals), 1)

    def test_policy_table_covers_every_intent(self):
        self.assertEqual(set(INTENT_DISPOSITIONS), {
            KNOWLEDGE_FOLLOWUP, STEP_EXPLANATION, ANSWER_REQUEST
        })


class CliSmokeTest(unittest.TestCase):
    def test_end_to_end_cli_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "state.json")
            fixture = str(Path("fixtures/intent_records.json"))
            env = {"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")}

            def run(*arguments):
                completed = subprocess.run(
                    [sys.executable, "-m", "nlp_classroom_intent_router.cli", *arguments,
                     "--state", state],
                    capture_output=True, text=True, env=env, check=True,
                )
                return json.loads(completed.stdout)

            summary = run("ingest", fixture)
            self.assertEqual(summary["stored"], 6)

            pending = run("review", "--record", "intent-demo-04")
            self.assertEqual(pending["current"]["status"], PENDING)

            run("supplement", "--record", "intent-demo-04",
                "--context", "高一物理·力学单元作业，讲解步骤")
            completed_view = run("review", "--record", "intent-demo-04")
            self.assertEqual(completed_view["current"]["status"], CLASSIFIED)

            run("decide", "--record", "intent-demo-03",
                "--intent", "step_explanation", "--teacher", "teacher-lin")
            decided = run("review", "--record", "intent-demo-03")
            self.assertEqual(decided["current"]["source"], "teacher")

            audit = run("audit")
            self.assertTrue(audit["ok"], audit["issues"])


if __name__ == "__main__":
    unittest.main()
