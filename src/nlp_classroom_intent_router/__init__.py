"""本地课堂意图分流工具。"""
from .intents import (
    ANSWER_REQUEST,
    INTENT_DISPOSITIONS,
    KNOWLEDGE_FOLLOWUP,
    STEP_EXPLANATION,
    classify,
    classification_key,
)
from .workflow import TextWorkflow

__all__ = [
    "TextWorkflow",
    "classify",
    "classification_key",
    "KNOWLEDGE_FOLLOWUP",
    "STEP_EXPLANATION",
    "ANSWER_REQUEST",
    "INTENT_DISPOSITIONS",
]
