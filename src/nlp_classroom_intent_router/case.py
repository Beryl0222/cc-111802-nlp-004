"""判题案件的事件流与处置生命周期。

所有结论与处置都以追加事件的形式保留，不在原事件上改写：
分类修订（含重放）、教师改判、处置发出与撤回各有事件类型，
教师复核视图与一致性审计都基于同一条事件流重建。
"""
import uuid

from .intents import (
    CLASSIFIER_SOURCE,
    INTENT_LABELS,
    INTENTS,
    PENDING,
)

# 事件类型
EVENT_CLASSIFICATION = "classification"
EVENT_TEACHER_DECISION = "teacher_decision"
EVENT_DISPOSITION_ISSUED = "disposition_issued"
EVENT_DISPOSITION_WITHDRAWN = "disposition_withdrawn"
EVENT_RECONCILIATION = "reconciliation"

# 处置状态
DISP_ISSUED = "issued"
DISP_WITHDRAWN = "withdrawn"

# 结论来源
SOURCE_TEACHER = "teacher"
SOURCE_PENDING = "pending"


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def build_event(event_type, timestamp, details):
    event = {"event_id": new_id("evt"), "event_type": event_type, "at": timestamp}
    event.update(details)
    return event


def make_case(record, key, timestamp):
    """为首次出现的判题键建立案件。"""
    return {
        "case_id": new_id("case"),
        "classification_key": key,
        "record_id": record["record_id"],
        "created_at": timestamp,
        "active_key": key,
        "events": [],
        "dispositions": [],
    }


def make_disposition(disposition_type, intent, issued_event_id, timestamp):
    return {
        "disposition_id": new_id("disp"),
        "type": disposition_type,
        "intent": intent,
        "status": DISP_ISSUED,
        "issued_event_id": issued_event_id,
        "withdrawn_event_id": None,
        "issued_at": timestamp,
        "withdrawn_at": None,
        "withdrawn_reason": None,
    }


def append_classification(case, result, key, timestamp, *, replayed=False):
    """追加一条分类事件。重放只留痕，不覆盖既有结论。"""
    event = build_event(
        EVENT_CLASSIFICATION,
        timestamp,
        {
            "source": CLASSIFIER_SOURCE,
            "classifier_version": result["classifier_version"],
            "status": result["status"],
            "intent": result["intent"],
            "reason": result["reason"],
            "matched_terms": list(result["matched_terms"]),
            "classification_key": key,
            "replayed": bool(replayed),
        },
    )
    case["events"].append(event)
    return event


def append_reconciliation(case, reason, timestamp, *, replayed=False,
                          supersedes_event_id=None):
    event = build_event(
        EVENT_RECONCILIATION,
        timestamp,
        {
            "reason": reason,
            "replayed": bool(replayed),
            "supersedes_event_id": supersedes_event_id,
        },
    )
    case["events"].append(event)
    return event


def close_disposition(disposition, reason, event_id, timestamp):
    disposition["status"] = DISP_WITHDRAWN
    disposition["withdrawn_event_id"] = event_id
    disposition["withdrawn_at"] = timestamp
    disposition["withdrawn_reason"] = reason


def current_conclusion(case):
    """从事件流重建当前结论：教师改判优先，否则取最新分类事件。"""
    teacher = None
    classification = None
    for event in case["events"]:
        if event["event_type"] == EVENT_TEACHER_DECISION:
            teacher = event
        elif event["event_type"] == EVENT_CLASSIFICATION:
            classification = event
    if teacher is not None:
        return {
            "status": teacher["status"],
            "intent": teacher["intent"],
            "source": SOURCE_TEACHER,
            "reason": teacher["reason"],
            "teacher": teacher["teacher"],
            "event_id": teacher["event_id"],
            "at": teacher["at"],
        }
    if classification is not None:
        return {
            "status": classification["status"],
            "intent": classification["intent"],
            "source": classification["source"],
            "reason": classification["reason"],
            "teacher": None,
            "event_id": classification["event_id"],
            "at": classification["at"],
        }
    return {
        "status": PENDING,
        "intent": None,
        "source": SOURCE_PENDING,
        "reason": "尚无任何判定事件",
        "teacher": None,
        "event_id": None,
        "at": None,
    }


def active_dispositions(case):
    return [item for item in case["dispositions"] if item["status"] == DISP_ISSUED]


def validate_intent(intent):
    if intent not in INTENTS:
        allowed = "、".join(INTENTS)
        raise ValueError(f"未知意图标签：{intent}，允许值：{allowed}")


def intent_label(intent):
    return INTENT_LABELS.get(intent, intent)
