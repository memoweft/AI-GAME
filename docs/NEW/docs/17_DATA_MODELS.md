# 17 数据模型

## 1. 范围与约定

本文定义 v0.1 的逻辑数据模型草案，不指定数据库产品、ORM 或最终表结构。字段名称用于统一 Runtime、Gateway、前端与测试语义。

通用约定：

- ID 使用不透明字符串；
- 时间使用带时区的 ISO 8601 UTC 字符串；
- 核心记录包含 `schema_version`；
- 大对象以引用保存；
- 模型推测与已验证事实必须区分；
- Event Store 事件按 Task 使用单调递增 `sequence`。

## 2. Task

```yaml
Task:
  id: string
  schema_version: integer
  goal: string
  status: CREATED | PLANNING | RUNNING | WAITING | PAUSED | STUCK | COMPLETED | FAILED | CANCELLED
  source:
    client_id: string
    conversation_id: string
    initial_message_id: string
  device_id: string
  constraints: Constraint[]
  current_stage_id: string | null
  completed_stage_ids: string[]
  fact_ids: string[]
  last_observation_id: string | null
  last_meaningful_progress_at: datetime | null
  failure_state: FailureState | null
  latest_checkpoint_id: string | null
  created_at: datetime
  updated_at: datetime
  terminal_at: datetime | null
```

`Task` 是 Snapshot，不直接存放全部历史。历史由 Event 提供。

## 3. Constraint

```yaml
Constraint:
  id: string
  text: string
  kind: user_instruction | runtime_limit | environment
  active: boolean
  source_message_id: string | null
  created_at: datetime
```

MVP 不要求复杂规则表达式。自然语言约束可保留原文，并由 Runtime 在角色调用时提供。

## 4. Stage

```yaml
Stage:
  id: string
  task_id: string
  ordinal: integer
  objective: string
  completion_criteria: string[]
  status: PENDING | ACTIVE | COMPLETED | FAILED | SUPERSEDED
  planner_call_id: string | null
  started_at: datetime | null
  completed_at: datetime | null
  evidence_refs: string[]
```

Stage 是阶段目标，不包含预先生成的 Action 列表。

## 5. Observation

```yaml
Observation:
  id: string
  task_id: string
  device_id: string
  captured_at: datetime
  screenshot:
    artifact_ref: string
    width: integer
    height: integer
    mime_type: string
  ui_tree:
    artifact_ref: string | null
    available: boolean
    error_code: string | null
  device_state:
    foreground_app: string | null
    screen_size: [integer, integer]
    orientation: portrait | landscape | unknown
    keyboard_state: shown | hidden | unknown
    connection_state: connected | disconnected | unauthorized | unknown
  consistency:
    status: consistent | degraded
    reason: string | null
```

## 6. Action

```yaml
Action:
  id: string
  task_id: string
  stage_id: string
  based_on_observation_id: string
  type: tap | long_press | swipe | input_text | back | home | open_app | wait | screenshot
  params: object
  expected_outcome: string
  proposed_by_call_id: string
  proposed_at: datetime
```

示例：

```json
{
  "id": "act_01",
  "task_id": "task_01",
  "stage_id": "stage_02",
  "based_on_observation_id": "obs_08",
  "type": "tap",
  "params": {"x": 824, "y": 1680},
  "expected_outcome": "进入当前电池相关条目的详情页面",
  "proposed_by_call_id": "call_15",
  "proposed_at": "2026-08-10T00:00:00Z"
}
```

## 7. ActionExecution

```yaml
ActionExecution:
  id: string
  action_id: string
  device_id: string
  accepted: boolean
  adapter_code: integer | null
  error:
    code: string
    message: string
    retryable: boolean
  started_at: datetime
  finished_at: datetime
```

`accepted` 仅表达传输/执行层结果。

## 8. Verification

```yaml
Verification:
  id: string
  task_id: string
  stage_id: string
  action_id: string
  before_observation_id: string
  after_observation_id: string
  verdict: SUCCESS | FAIL | UNCERTAIN
  reason: string
  evidence_refs: string[]
  method: runtime_rule | role_assisted | combined
  verification_call_id: string | null
  created_at: datetime
```

## 9. Fact

```yaml
Fact:
  id: string
  task_id: string
  key: string
  value: any
  status: VERIFIED | USER_PROVIDED | UNVERIFIED
  confidence: number | null
  source_refs: string[]
  created_at: datetime
  supersedes_fact_id: string | null
```

例：电池页面可见信息可以保存为 `key=battery.visible_text` 或进一步解析的字段，但最终答复必须能追溯到 Observation。

## 10. RoleCall 与 RoleBinding

```yaml
RoleBinding:
  role: planner | operator | language
  adapter_id: string
  execution_class: local | cloud | test
  config_revision: string

RoleCall:
  id: string
  task_id: string
  role: planner | operator | language
  binding: RoleBinding
  input_refs: string[]
  output_type: string
  status: SUCCEEDED | FAILED | TIMED_OUT | REJECTED
  latency_ms: integer | null
  error_code: string | null
  created_at: datetime
```

`adapter_id` 是配置标识，不构成公共模型供应商契约。

## 11. RuntimeEvent

```yaml
RuntimeEvent:
  id: string
  task_id: string
  sequence: integer
  type: string
  actor: user | gateway | runtime | model | device
  payload: object
  causation_id: string | null
  correlation_id: string | null
  created_at: datetime
  schema_version: integer
```

## 12. Checkpoint

```yaml
Checkpoint:
  id: string
  task_id: string
  through_sequence: integer
  goal: string
  constraints: Constraint[]
  status: string
  current_stage: Stage | null
  completed_stage_summaries: object[]
  verified_facts: Fact[]
  device_summary: object
  last_meaningful_progress: object | null
  failure_summary: object | null
  created_at: datetime
```

## 13. ClientMessage

```yaml
ClientMessage:
  id: string
  client_id: string
  conversation_id: string
  task_id: string | null
  text: string
  explicit_control: pause | resume | cancel | takeover | null
  received_at: datetime
  idempotency_key: string
```

## 14. FailureState

```yaml
FailureState:
  code: string
  summary: string
  retry_count: integer
  no_progress_count: integer
  last_failed_action_id: string | null
  last_verdict: FAIL | UNCERTAIN | null
  recoverable: boolean
  updated_at: datetime
```

## 15. 数据关系

```text
Task 1 ── * Stage
Task 1 ── * Observation
Task 1 ── * Action ── 0..1 ActionExecution ── 0..1 Verification
Task 1 ── * Fact
Task 1 ── * RuntimeEvent
Task 1 ── * RoleCall
Task 1 ── * Checkpoint
```

## 16. 持久化边界

必须持久化：Task、关键 Event、Stage、已验证 Fact、Checkpoint 元数据。

可按配置保留：截图、UI Tree、模型原始响应、ADB 详细日志。

不作为产品数据持久化：模型冗长思考过程、无界 Token 流、前端本地推断出的第二套任务状态。
