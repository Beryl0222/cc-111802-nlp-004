import argparse
import json
from pathlib import Path

from .intents import INTENTS
from .repository import RecordRepository
from .workflow import TextWorkflow


def build_parser():
    parser = argparse.ArgumentParser(description="本地课堂意图分流工具")
    parser.add_argument("action", choices=(
        "ingest", "list", "review", "decide", "supplement", "audit"
    ))
    parser.add_argument("path", nargs="?", default="fixtures/records.json",
                        help="ingest 时为导入文件路径；其他动作可省略")
    parser.add_argument("--state", default="var/records.json")
    parser.add_argument("--record", help="review 指定的记录编号")
    parser.add_argument("--source", help="按来源过滤")
    parser.add_argument("--intent", choices=INTENTS, help="decide：教师改判的意图")
    parser.add_argument("--teacher", help="decide：改判教师标识")
    parser.add_argument("--reason", help="decide：改判理由")
    parser.add_argument("--context", help="supplement：补齐的课程上下文")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    workflow = TextWorkflow(RecordRepository(args.state))

    if args.action == "ingest":
        result = workflow.ingest(
            json.loads(Path(args.path).read_text(encoding="utf-8"))
        )
    elif args.action == "list":
        result = workflow.list_records(source=args.source)
    elif args.action == "review":
        result = (workflow.review_view(args.record) if args.record
                  else workflow.review_view(source=args.source))
    elif args.action == "decide":
        if not args.record or not args.intent or not args.teacher:
            parser.error("decide 需要 --record、--intent 与 --teacher")
        result = workflow.teacher_decide(
            args.record, args.intent, args.teacher, reason=args.reason
        )
    elif args.action == "supplement":
        if not args.record or not args.context:
            parser.error("supplement 需要 --record 与 --context")
        result = workflow.supplement_context(args.record, args.context)
    else:  # audit
        result = workflow.audit()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
