# 课堂意图分流器

课堂意图分流器用于处理本地合成教研文本，保存来源版本、文本片段、意图判定事件与处置记录，供校内复核使用。

## 意图与处置

每条课堂请求只对应一个当前意图标签：

| 意图 | 含义 | 对应处置通道 |
| --- | --- | --- |
| `knowledge_followup` | 追问知识 | 学生提示 `student_hint` |
| `step_explanation` | 要求解释步骤 | 人工复核 `manual_review` |
| `answer_request` | 请求直接给出作业答案 | 教师提醒 `teacher_reminder` |

上下文不足或规则信号冲突时，请求保持**待判（pending）**，不发出任何处置；补齐课程上下文后可继续处理。

## 记录与核对模型

- **判题键**：由「文本 + 课程上下文」共同派生（`classification_key`）。同一文本在不同课程任务中属于不同判题范围，不能只按文本去重；文本或上下文变更会重开判题，旧处置带原因撤回，再重新分类。
- **append-only 事件流**：分类判定（含分类器版本与命中词）、教师改判、处置发出、处置撤回、核对说明全部作为不可变事件追加，任何结论都能追溯到来源事件。
- **重放不回写**：重复导入只追加带 `replayed` 标记的分类事件和核对说明，绝不覆盖当前结论；教师改判优先级始终高于分类器，旧标签无法写回。
- **改判联动处置**：教师改判后，与新策略不符的已发处置带原因撤回，策略一致的处置保留并记录核对说明。
- **一致性审计**：`audit` 重建判题键、事件引用与处置状态，发现断链或悬空处置时逐条报告。

运行数据保存在 `var/records.json`，包含 `records`（文本与分析）与 `cases`（事件流与处置）两个部分。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 运行

```bash
# 导入演练记录（同文件可重复导入，重放不会覆盖结论）
PYTHONPATH=src python -m nlp_classroom_intent_router.cli ingest fixtures/intent_records.json

# 查看记录与教师复核视图
PYTHONPATH=src python -m nlp_classroom_intent_router.cli list
PYTHONPATH=src python -m nlp_classroom_intent_router.cli review
PYTHONPATH=src python -m nlp_classroom_intent_router.cli review --record intent-demo-03

# 教师改判（必须提供改判教师与意图）
PYTHONPATH=src python -m nlp_classroom_intent_router.cli decide \
  --record intent-demo-03 --intent step_explanation --teacher teacher-lin --reason "学生在问解题过程"

# 为待判请求补齐课程上下文，触发重新分类
PYTHONPATH=src python -m nlp_classroom_intent_router.cli supplement \
  --record intent-demo-04 --context "高一物理·力学单元作业，讲解步骤"

# 一致性审计：分类修订、改判与已发处置互相核对
PYTHONPATH=src python -m nlp_classroom_intent_router.cli audit
```

通用参数：`--state` 指定状态文件（默认 `var/records.json`），`--source` 按来源过滤。

## 测试

执行 `python -m pytest -q` 检查文本规范化、规则分类、待判与补齐、教师改判、重放不回写、判题范围重开、处置撤回/保留、复核视图、一致性审计、本地持久化及 CLI 端到端流程。
