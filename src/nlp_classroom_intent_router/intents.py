"""意图标签、处置策略与规则分类器。

分类器只依赖标准库，输出带版本号，便于在事件流中追溯结论来源。
判题身份由「文本 + 课程上下文」共同决定，不能只按文本去重。
"""
import hashlib

from .text import normalize_text

# 意图标签
KNOWLEDGE_FOLLOWUP = "knowledge_followup"
STEP_EXPLANATION = "step_explanation"
ANSWER_REQUEST = "answer_request"
INTENTS = (KNOWLEDGE_FOLLOWUP, STEP_EXPLANATION, ANSWER_REQUEST)
INTENT_LABELS = {
    KNOWLEDGE_FOLLOWUP: "追问知识",
    STEP_EXPLANATION: "要求解释步骤",
    ANSWER_REQUEST: "请求直接给出作业答案",
}

# 处置通道
STUDENT_HINT = "student_hint"
TEACHER_REMINDER = "teacher_reminder"
MANUAL_REVIEW = "manual_review"
DISPOSITION_LABELS = {
    STUDENT_HINT: "学生提示",
    TEACHER_REMINDER: "教师提醒",
    MANUAL_REVIEW: "人工复核",
}

# 分类结论 -> 处置通道的固定策略表
INTENT_DISPOSITIONS = {
    KNOWLEDGE_FOLLOWUP: STUDENT_HINT,
    STEP_EXPLANATION: MANUAL_REVIEW,
    ANSWER_REQUEST: TEACHER_REMINDER,
}

CLASSIFIER_VERSION = "rule-based-v1"
CLASSIFIER_SOURCE = f"classifier:{CLASSIFIER_VERSION}"

CLASSIFIED = "classified"
PENDING = "pending"

# (意图, 权重, 关键词)。直接索取答案、要求讲解步骤属于明确的任务型信号，
# 权重高于“是什么/为什么”等泛化疑问词；同分时再进入待判。
_RULES = (
    (
        ANSWER_REQUEST,
        2,
        ("答案", "直接给", "帮我写", "替我写", "帮我做", "替我做", "抄给", "抄一下", "写完", "整套"),
    ),
    (
        STEP_EXPLANATION,
        2,
        ("步骤", "过程", "思路", "怎么算", "怎么用", "如何解", "讲解", "讲一下", "推导", "一步步", "解法"),
    ),
    (
        KNOWLEDGE_FOLLOWUP,
        1,
        ("是什么", "什么是", "为什么", "原理", "区别", "含义", "定义", "意思", "哪些"),
    ),
)


def classification_key(text, context):
    """生成判题键：同一文本在不同课程任务中属于不同判题范围。"""
    payload = normalize_text(text) + "\u001f" + normalize_text(context)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def classify(text, context):
    """对单条请求做规则分类。

    返回结构包含状态、意图（待判时为 None）、理由与命中的规则词，
    调用方负责把结果原样写入事件流。
    """
    normalized = normalize_text(text)
    context_text = normalize_text(context)
    if not context_text:
        return {
            "status": PENDING,
            "intent": None,
            "reason": "缺少课程上下文，无法确认请求所处的课程任务，保持待判",
            "matched_terms": [],
            "classifier_version": CLASSIFIER_VERSION,
        }

    # 规则词同时在请求文本与课程上下文中匹配：
    # 课程任务（如“作业，讲解步骤”）也是意图判断的依据。
    searchable = f"{normalized} {context_text}"
    scores = {}
    matched = {}
    for intent, weight, keywords in _RULES:
        hits = [keyword for keyword in keywords if keyword in searchable]
        if hits:
            scores[intent] = weight * len(hits)
            matched[intent] = hits

    if not scores:
        return {
            "status": PENDING,
            "intent": None,
            "reason": "未命中任何意图规则，需要人工判定",
            "matched_terms": [],
            "classifier_version": CLASSIFIER_VERSION,
        }

    top_score = max(scores.values())
    winners = [intent for intent, score in scores.items() if score == top_score]
    if len(winners) > 1:
        labels = "、".join(INTENT_LABELS[intent] for intent in winners)
        terms = sorted({term for terms in matched.values() for term in terms})
        return {
            "status": PENDING,
            "intent": None,
            "reason": f"规则同时指向{labels}（命中：{'、'.join(terms)}），信号冲突，待人工判定",
            "matched_terms": terms,
            "classifier_version": CLASSIFIER_VERSION,
        }

    intent = winners[0]
    return {
        "status": CLASSIFIED,
        "intent": intent,
        "reason": f"分类器 {CLASSIFIER_VERSION} 命中规则词：{'、'.join(matched[intent])}",
        "matched_terms": matched[intent],
        "classifier_version": CLASSIFIER_VERSION,
    }
