"""意图分流工作流。

设计要点：
- 判题身份 = 文本 + 课程上下文（classification_key），上下文变更即重开判题，
  同一文本在不同课程任务中互不覆盖。
- 分类、教师改判、处置发出/撤回、重放核对全部写入 append-only 事件流，
  重放只追加留痕，绝不把旧标签写回当前结论；教师改判优先级最高。
- 上下文不足或规则冲突时保持待判（pending），补齐上下文后可继续处理。
"""
from datetime import datetime, timezone

from .case import (
    DISP_ISSUED,
    DISP_WITHDRAWN,
    EVENT_CLASSIFICATION,
    EVENT_DISPOSITION_ISSUED,
    EVENT_DISPOSITION_WITHDRAWN,
    EVENT_RECONCILIATION,
    EVENT_TEACHER_DECISION,
    SOURCE_TEACHER,
    active_dispositions,
    append_classification,
    append_reconciliation,
    build_event,
    close_disposition,
    current_conclusion,
    intent_label,
    make_case,
    make_disposition,
    validate_intent,
)
from .intents import (
    CLASSIFIED,
    DISPOSITION_LABELS,
    INTENT_DISPOSITIONS,
    INTENT_LABELS,
    classify,
    classification_key,
)
from .records import TextRecord
from .text import annotate, normalize_text

_EVENT_SUMMARIES = {
    EVENT_CLASSIFICATION: "分类判定",
    EVENT_TEACHER_DECISION: "教师改判",
    EVENT_DISPOSITION_ISSUED: "处置发出",
    EVENT_DISPOSITION_WITHDRAWN: "处置撤回",
    EVENT_RECONCILIATION: "核对说明",
}


class TextWorkflow:
    def __init__(self, repository, clock=None):
        self.repository = repository
        self.clock = clock

    def now(self):
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------------ 导入
    def ingest(self, values):
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        incoming = []
        for value in values:
            record = TextRecord.from_dict(value).to_dict()
            record["text"] = normalize_text(record["text"])
            record["context"] = normalize_text(record["context"])
            incoming.append(record)

        records, cases = self.repository.read_all()
        record_index = {row["record_id"]: row for row in records}
        case_index = {row["record_id"]: row for row in cases}

        for record in incoming:
            timestamp = self.now()
            existing = record_index.get(record["record_id"])
            if existing is not None and record["revision"] < existing["revision"]:
                # 过期修订号：整条忽略，旧结论与处置保持不变
                continue

            record["analysis"] = annotate(record)
            key = classification_key(record["text"], record["context"])
            case = case_index.get(record["record_id"])

            if case is None:
                case = make_case(record, key, timestamp)
                cases.append(case)
                case_index[record["record_id"]] = case
                self._run_classification(case, record, timestamp)
            elif key != case["active_key"]:
                self._reopen_scope(case, record, key, timestamp)
            else:
                self._replay_classification(case, record, key, timestamp)

            record_index[record["record_id"]] = record

        records = sorted(record_index.values(), key=lambda row: row["record_id"])
        cases = sorted(case_index.values(), key=lambda row: row["record_id"])
        self.repository.replace_all(records, cases)
        return {"received": len(incoming), "stored": len(records)}

    def list_records(self, source=None):
        records, _ = self.repository.read_all()
        if source:
            records = [record for record in records if record["source"] == source]
        return records

    # ------------------------------------------------------------ 教师改判
    def teacher_decide(self, record_id, intent, teacher, reason=None):
        validate_intent(intent)
        if not str(teacher or "").strip():
            raise ValueError("教师改判必须记录改判人")
        records, cases = self._load_indexed()
        record, case = self._require_record_case(record_id, records, cases)
        timestamp = self.now()

        previous = current_conclusion(case)
        event = build_event(
            EVENT_TEACHER_DECISION,
            timestamp,
            {
                "teacher": str(teacher).strip(),
                "status": CLASSIFIED,
                "intent": intent,
                "reason": str(reason).strip() if reason else f"教师改判为{intent_label(intent)}",
                "previous_event_id": previous["event_id"],
                "previous_intent": previous["intent"],
                "classification_key": case["active_key"],
            },
        )
        case["events"].append(event)
        self._reconcile_dispositions(case, intent, event, timestamp)

        self._save(records, cases)
        return self.review_view(record_id)

    # ------------------------------------------------------------ 补充上下文
    def supplement_context(self, record_id, context):
        normalized = normalize_text(context)
        if not normalized:
            raise ValueError("补充的课程上下文不能为空")
        records, cases = self._load_indexed()
        record, case = self._require_record_case(record_id, records, cases)
        timestamp = self.now()

        record["context"] = normalized
        record["analysis"] = annotate(record)
        key = classification_key(record["text"], record["context"])
        if key == case["active_key"]:
            append_reconciliation(case, "补充的课程上下文与现有上下文一致，维持当前结论", timestamp)
        else:
            self._reopen_scope(case, record, key, timestamp)

        self._save(records, cases)
        return self.review_view(record_id)

    # ------------------------------------------------------------ 复核视图
    def review_view(self, record_id=None, source=None):
        records, cases = self._load_indexed()
        case_index = {row["record_id"]: row for row in cases}
        if record_id is not None:
            record, case = self._require_record_case(record_id, records, cases)
            return self._build_view(record, case)

        views = []
        for record in records:
            if source and record["source"] != source:
                continue
            case = case_index.get(record["record_id"])
            if case is None:
                continue
            views.append(self._build_view(record, case))
        return views

    def _build_view(self, record, case):
        conclusion = current_conclusion(case)
        intent = conclusion["intent"]
        return {
            "record_id": record["record_id"],
            "source": record["source"],
            "revision": record["revision"],
            "observed_at": record["observed_at"],
            "text": record["text"],
            "context": record["context"],
            "classification_key": case["active_key"],
            "current": {
                "status": conclusion["status"],
                "intent": intent,
                "intent_label": INTENT_LABELS.get(intent) if intent else None,
                "source": conclusion["source"],
                "reason": conclusion["reason"],
                "teacher": conclusion["teacher"],
                "event_id": conclusion["event_id"],
                "at": conclusion["at"],
            },
            "active_dispositions": [
                self._disposition_summary(item) for item in active_dispositions(case)
            ],
            "disposition_history": [
                self._disposition_summary(item) for item in case["dispositions"]
            ],
            "timeline": [self._event_summary(event) for event in case["events"]],
        }

    @staticmethod
    def _disposition_summary(item):
        return {
            "disposition_id": item["disposition_id"],
            "type": item["type"],
            "type_label": DISPOSITION_LABELS.get(item["type"], item["type"]),
            "intent": item["intent"],
            "intent_label": INTENT_LABELS.get(item["intent"], item["intent"]),
            "status": item["status"],
            "issued_event_id": item["issued_event_id"],
            "issued_at": item["issued_at"],
            "withdrawn_event_id": item["withdrawn_event_id"],
            "withdrawn_at": item["withdrawn_at"],
            "withdrawn_reason": item["withdrawn_reason"],
        }

    @staticmethod
    def _event_summary(event):
        event_type = event["event_type"]
        summary = {"event_id": event["event_id"], "at": event["at"],
                   "event_type": event_type, "event_label": _EVENT_SUMMARIES.get(event_type, event_type)}
        if event_type == EVENT_CLASSIFICATION:
            summary.update({
                "source": event["source"],
                "status": event["status"],
                "intent": event["intent"],
                "intent_label": INTENT_LABELS.get(event["intent"]) if event["intent"] else None,
                "reason": event["reason"],
                "replayed": event["replayed"],
                "classification_key": event["classification_key"],
            })
        elif event_type == EVENT_TEACHER_DECISION:
            summary.update({
                "teacher": event["teacher"],
                "intent": event["intent"],
                "intent_label": INTENT_LABELS.get(event["intent"]),
                "reason": event["reason"],
                "previous_event_id": event["previous_event_id"],
            })
        elif event_type == EVENT_DISPOSITION_ISSUED:
            summary.update({
                "type": event["type"],
                "type_label": DISPOSITION_LABELS.get(event["type"], event["type"]),
                "basis_event_id": event["basis_event_id"],
            })
        elif event_type == EVENT_DISPOSITION_WITHDRAWN:
            summary.update({
                "disposition_id": event["disposition_id"],
                "type": event["type"],
                "reason": event["reason"],
                "basis_event_id": event["basis_event_id"],
            })
        else:  # 核对说明
            summary.update({
                "reason": event["reason"],
                "replayed": event["replayed"],
                "supersedes_event_id": event["supersedes_event_id"],
            })
        return summary

    # ------------------------------------------------------------ 一致性审计
    def audit(self):
        records, cases = self._load_indexed()
        record_index = {row["record_id"]: row for row in records}
        issues = []

        for case in cases:
            record = record_index.get(case["record_id"])
            if record is None:
                issues.append(f"案件 {case['case_id']} 找不到对应记录 {case['record_id']}")
                continue

            expected_key = classification_key(record["text"], record["context"])
            known_keys = {event["classification_key"] for event in case["events"]
                          if event.get("classification_key")}
            if expected_key != case["active_key"]:
                issues.append(
                    f"记录 {record['record_id']} 的判题键与当前文本/上下文不一致："
                    f"存储 {case['active_key']}，重算 {expected_key}"
                )
            if expected_key not in known_keys:
                issues.append(
                    f"记录 {record['record_id']} 当前判题键 {expected_key} 在事件流中没有对应判定事件"
                )

            event_ids = {event["event_id"] for event in case["events"]}
            for disposition in case["dispositions"]:
                if disposition["issued_event_id"] not in event_ids:
                    issues.append(f"处置 {disposition['disposition_id']} 的发出事件缺失")
                if disposition["status"] == DISP_WITHDRAWN:
                    if disposition["withdrawn_event_id"] not in event_ids:
                        issues.append(f"处置 {disposition['disposition_id']} 的撤回事件缺失")
                    if not disposition["withdrawn_at"] or not disposition["withdrawn_reason"]:
                        issues.append(f"处置 {disposition['disposition_id']} 撤回信息不完整")
                elif disposition["status"] != DISP_ISSUED:
                    issues.append(f"处置 {disposition['disposition_id']} 状态非法：{disposition['status']}")

            conclusion = current_conclusion(case)
            active = active_dispositions(case)
            if conclusion["status"] == CLASSIFIED:
                wanted = INTENT_DISPOSITIONS[conclusion["intent"]]
                active_types = sorted(item["type"] for item in active)
                if active_types != [wanted]:
                    issues.append(
                        f"记录 {record['record_id']} 当前结论为"
                        f"{intent_label(conclusion['intent'])}，有效处置应为 {wanted}，"
                        f"实际为 {active_types or '空'}"
                    )
            elif active:
                issues.append(f"记录 {record['record_id']} 处于待判状态，却存在 {len(active)} 条有效处置")

            for event in case["events"]:
                # 判题范围重开后，旧事件保留旧判题键属正常现象；
                # 只有处置生命周期事件不携带判题键，跳过不查。
                if event["event_type"] not in (
                    EVENT_CLASSIFICATION, EVENT_TEACHER_DECISION
                ):
                    continue
                if not event.get("classification_key"):
                    issues.append(f"事件 {event['event_id']} 缺少判题键")
                elif event["classification_key"] not in known_keys:
                    issues.append(
                        f"事件 {event['event_id']} 的判题键 {event['classification_key']} "
                        "不属于该案件的任何判题范围"
                    )

        case_record_ids = {case["record_id"] for case in cases}
        for record in records:
            if record["record_id"] not in case_record_ids:
                issues.append(f"记录 {record['record_id']} 缺少判题案件")

        return {"ok": not issues, "issues": issues,
                "records_checked": len(records), "cases_checked": len(cases)}

    # ------------------------------------------------------------ 内部辅助
    def _run_classification(self, case, record, timestamp):
        result = classify(record["text"], record["context"])
        event = append_classification(case, result, case["active_key"], timestamp)
        if result["status"] == CLASSIFIED:
            self._issue_disposition(case, result["intent"], event["event_id"], timestamp)
        return event

    def _replay_classification(self, case, record, key, timestamp):
        result = classify(record["text"], record["context"])
        event = append_classification(case, result, key, timestamp, replayed=True)
        conclusion = current_conclusion(case)

        if conclusion["source"] == SOURCE_TEACHER:
            reason = (
                f"重放分类（{result['status']}"
                + (f"：{intent_label(result['intent'])}" if result["intent"] else "")
                + f"）不回写；当前保留教师改判结论：{intent_label(conclusion['intent'])}"
            )
        elif result["status"] == conclusion["status"] and result["intent"] == conclusion["intent"]:
            reason = "重放分类与当前结论一致，当前结论与已发处置保留"
        else:
            reason = (
                "重放分类与当前结论不一致，按规定保留当前结论与已发处置，"
                "重放标签仅留痕，差异请教师复核"
            )
        append_reconciliation(
            case, reason, timestamp, replayed=True, supersedes_event_id=event["event_id"]
        )

    def _reopen_scope(self, case, record, key, timestamp):
        previous_key = case["active_key"]
        note = append_reconciliation(
            case,
            f"文本或课程上下文变更，判题范围改变（{previous_key} → {key}）；"
            "原范围内已发处置全部撤回，并按新范围重新分类",
            timestamp,
        )
        for disposition in active_dispositions(case):
            reason = "判题范围（文本/课程上下文）变更，原处置撤回"
            close_disposition(disposition, reason, note["event_id"], timestamp)
            case["events"].append(build_event(
                EVENT_DISPOSITION_WITHDRAWN,
                timestamp,
                {
                    "disposition_id": disposition["disposition_id"],
                    "type": disposition["type"],
                    "reason": reason,
                    "basis_event_id": note["event_id"],
                },
            ))
        case["active_key"] = key
        self._run_classification(case, record, timestamp)

    def _reconcile_dispositions(self, case, new_intent, decision_event, timestamp):
        wanted = INTENT_DISPOSITIONS[new_intent]
        retained = []
        for disposition in active_dispositions(case):
            if disposition["type"] == wanted:
                retained.append(disposition)
                continue
            reason = (
                f"教师改判为{intent_label(new_intent)}，处置策略改为"
                f"{DISPOSITION_LABELS[wanted]}，原{DISPOSITION_LABELS[disposition['type']]}撤回"
            )
            close_disposition(disposition, reason, decision_event["event_id"], timestamp)
            case["events"].append(build_event(
                EVENT_DISPOSITION_WITHDRAWN,
                timestamp,
                {
                    "disposition_id": disposition["disposition_id"],
                    "type": disposition["type"],
                    "reason": reason,
                    "basis_event_id": decision_event["event_id"],
                },
            ))

        if retained:
            ids = "、".join(item["disposition_id"] for item in retained)
            append_reconciliation(
                case,
                f"已发{DISPOSITION_LABELS[wanted]}与改判后的处置策略一致，予以保留（处置号：{ids}）",
                timestamp,
                supersedes_event_id=decision_event["event_id"],
            )
        else:
            self._issue_disposition(case, new_intent, decision_event["event_id"], timestamp)

    @staticmethod
    def _issue_disposition(case, intent, basis_event_id, timestamp):
        disposition_type = INTENT_DISPOSITIONS[intent]
        issued = build_event(
            EVENT_DISPOSITION_ISSUED,
            timestamp,
            {
                "type": disposition_type,
                "intent": intent,
                "basis_event_id": basis_event_id,
            },
        )
        case["events"].append(issued)
        case["dispositions"].append(
            make_disposition(disposition_type, intent, issued["event_id"], timestamp)
        )

    def _load_indexed(self):
        records, cases = self.repository.read_all()
        return records, cases

    def _save(self, records, cases):
        records = sorted(records, key=lambda row: row["record_id"])
        cases = sorted(cases, key=lambda row: row["record_id"])
        self.repository.replace_all(records, cases)

    @staticmethod
    def _require_record_case(record_id, records, cases):
        record = next((row for row in records if row["record_id"] == record_id), None)
        if record is None:
            raise KeyError(f"找不到记录：{record_id}")
        case = next((row for row in cases if row["record_id"] == record_id), None)
        if case is None:
            raise KeyError(f"记录 {record_id} 尚未建立判题案件，请先导入")
        return record, case
