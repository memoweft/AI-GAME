# Phase 1 — Data Model Design

> 状态：FROZEN DESIGN  
> 日期：2026-08-10  
> 数据库实现：NOT STARTED  
> 目标数据库：`runtime/console/runtime.db`

## 1. 设计原则

1. 新 Runtime 使用独立 Store，不复用或迁移旧 `mobile-tasks.db` schema。
2. Task Snapshot 是当前事实投影；历史由追加式 Event 提供。
3. Goal、Stage、Action 只有三层，不保存预生成 Action 列表或任务 DAG。
4. Observation、Transport Result、Verification 和 Commit 是不同事实。
5. 模型建议、用户提供事实和 Runtime 验证事实必须可区分。
6. 大对象只保存引用；截图、UI Tree 和模型原始响应不内联进 Event。
7. 不保存模型长推理或无限聊天 Context。
8. 所有 Task Event 在 Task 内使用单调递增 sequence。
9. 时间使用带时区 ISO 8601 UTC；ID 使用不透明字符串。
10. 所有核心记录携带 `schema_version` 或受数据库 schema revision 管理。

## 2. Store 边界

目标数据库：

```text
F:\AI-GAME\runtime\console\runtime.db
```

旧数据库：

```text
F:\AI-GAME\runtime\console\mobile-tasks.db
```

旧库在 Device Ownership cutover 时冻结；新 Store 不通过 attach、view、trigger 或跨库 foreign key 依赖旧库。

逻辑表集合：

```text
runtime_schema
runtime_tasks
runtime_constraints
runtime_stages
runtime_observations
runtime_actions
runtime_action_executions
runtime_verifications
runtime_facts
runtime_role_calls
runtime_events
runtime_checkpoints
runtime_client_messages
runtime_idempotency
```

表名是设计方向；具体 DDL、索引和事务实现必须由后续 Store 施工单冻结并测试。

## 3. Task

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
  current_stage_id: string | null
  last_observation_id: string | null
  last_meaningful_progress_at: datetime | null
  failure_state: FailureState | null
  latest_checkpoint_id: string | null
  created_at: datetime
  updated_at: datetime
  terminal_at: datetime | null
```

Task 不直接内联所有 Constraint、Fact、Stage 或 Event。Gateway Task Snapshot 可以把相关投影组合为：

```text
goal
status
constraints[]
current_stage
completed_stages[]
facts[]
last_observation_ref
failure_state
checkpoint_ref
active_role_bindings
```

### Task 状态不变量

- 终态为 `COMPLETED / FAILED / CANCELLED`；终态不接受设备动作。
- `COMPLETED` 需要 Goal 和收尾动作均有验证证据。
- `UNCERTAIN` 是 Verification verdict，不是 Task 顶层状态。
- `PAUSED` 和 `WAITING` 不产生新自动 Action。
- `STUCK` 必须有无进度证据，不能仅由运行时间推断。

## 4. Constraint

```yaml
Constraint:
  id: string
  task_id: string
  text: string
  kind: user_instruction | runtime_limit | environment
  active: boolean
  source_message_id: string | null
  created_at: datetime
  deactivated_at: datetime | null
```

约束保留用户原文和来源，不在 MVP 建设复杂规则语言。激活/撤销通过 Event 记录。

## 5. Stage

```yaml
Stage:
  id: string
  task_id: string
  ordinal: integer
  objective: string
  completion_criteria: string[]
  status: PENDING | ACTIVE | COMPLETED | FAILED | SUPERSEDED
  planner_call_id: string | null
  progress_summary: string | null
  started_at: datetime | null
  completed_at: datetime | null
  evidence_refs: string[]
```

Phase 1 施工单中提出的 `Stage.goal` 在正式模型中命名为 `objective`，避免与 Task Goal 混淆。`progress` 是由 Verification/Event 投影出的短摘要，不是模型可任意递增的百分比。

不变量：

- 一个 Task 最多一个 `ACTIVE` Stage；
- Stage 不保存 Action 列表；
- Stage 完成必须引用成功 Verification 或等价可审计证据；
- 用户改变 Goal/关键约束后，旧 Stage 可以 `SUPERSEDED`，不能静默改写历史。

## 6. Observation

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

Observation 不包含模型对页面的成功判断。截图、UI Tree 与 Device State 采集间隔过大、方向变化或设备标识不一致时，必须标记 `degraded`。

## 7. Action

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
  validation_status: accepted | rejected
  rejection_code: string | null
```

Action 只表达提案。Phase 1 施工单中的 `result` 和 `verification` 拆为独立实体，避免把 transport 与业务成功混在同一字段。

## 8. ActionExecution

```yaml
ActionExecution:
  id: string
  action_id: string
  device_id: string
  lease_ref: string
  accepted: boolean
  adapter_code: integer | null
  error:
    code: string
    message: string
    retryable: boolean
  started_at: datetime
  finished_at: datetime
```

`accepted=true` 只代表设备传输层接受并结算请求，不代表 Expected Outcome、Stage 或 Task 成功。

## 9. Verification

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

不变量：

- `SUCCESS` 才能 Commit；
- `FAIL` 和 `UNCERTAIN` 不添加“已完成”Fact；
- `UNCERTAIN` 必须触发重新观察、替代验证或恢复逻辑，不能当作 SUCCESS；
- Verification 必须引用动作后的新 Observation。

## 10. Fact

```yaml
Fact:
  id: string
  task_id: string
  key: string
  value: any
  status: VERIFIED | USER_PROVIDED | UNVERIFIED
  confidence: number | null
  scope: task | stage
  source_refs: string[]
  created_at: datetime
  supersedes_fact_id: string | null
```

Phase 1 施工单要求的 `scope` 限定为 Task 或 Stage，不引入跨 Task 长期记忆作用域。

规则：

- Observation/Verification 产生的可信事实使用 `VERIFIED`；
- 用户明确提供、但设备未验证的事实使用 `USER_PROVIDED`；
- 模型建议只能使用 `UNVERIFIED`，且不得出现在最终设备事实反馈中；
- 事实变化创建新记录并通过 `supersedes_fact_id` 关联，不静默覆盖来源。

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

核心事件至少包括：

```text
TaskCreated
GoalUpdated
ConstraintAdded
ConstraintRemoved
StageStarted
ObservationReceived
ActionProposed
ActionExecuted
ActionVerified
FactAdded
StageCompleted
ModelEscalated
CheckpointCreated
UserIntervened
TaskPaused
TaskResumed
TaskCompleted
TaskFailed
TaskCancelled
```

约束：

- `(task_id, sequence)` 唯一；
- Event append 与对应状态改变处于同一 Store 事务；
- Event 使用已发生事实的过去时语义；
- 大对象通过 ref 引用；
- Event 不等同于日志。

## 12. Checkpoint

```yaml
Checkpoint:
  id: string
  task_id: string
  through_sequence: integer
  goal: string
  constraints: Constraint[]
  status_at_checkpoint: string
  current_stage: Stage | null
  completed_stage_summaries: object[]
  verified_facts: Fact[]
  device_summary: object
  last_meaningful_progress: object | null
  failure_summary: object | null
  resume_context:
    reason: string
    required_fresh_observation: boolean
    unresolved_action_ref: string | null
  created_at: datetime
```

恢复输入是：

```text
Goal + Checkpoint + events after through_sequence + Current Observation
```

Checkpoint 不包含完整模型 Context。若存在 unresolved physical action，`required_fresh_observation` 必须为 true，恢复不得重放该动作。

## 13. RoleBinding 与 RoleCall

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

`adapter_id` 是部署配置标识，不进入 Task 业务分支。模型原始响应可以按配置作为 Artifact 保存，但不是 Event 主体或长期产品事实。

## 14. ClientMessage

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

每条用户消息保留稳定来源；消息解释为 Goal/Constraint/Information/Control 后产生对应 Event。Gateway 不能仅凭“最近消息”猜测 Task。

## 15. FailureState

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

失败分类只服务恢复，不在 MVP 建设庞大错误本体。

## 16. IdempotencyRecord

```yaml
IdempotencyRecord:
  client_id: string
  key: string
  operation: string
  request_digest: string
  response_ref: string
  created_at: datetime
```

同一 Client/key/operation 和相同 digest 返回原结果；相同 key 对应不同 payload 返回 `IDEMPOTENCY_CONFLICT`。

## 17. 数据关系

```text
Task 1 ── * Constraint
Task 1 ── * Stage
Task 1 ── * Observation
Task 1 ── * Action ── 0..1 ActionExecution ── 0..1 Verification
Task 1 ── * Fact
Task 1 ── * RuntimeEvent
Task 1 ── * RoleCall
Task 1 ── * Checkpoint
Task 1 ── * ClientMessage
```

## 18. 事务边界

后续 Store 实现必须至少保证：

- Task create + `TaskCreated` + idempotency result 原子提交；
- 状态改变 + 对应 Event + sequence 原子提交；
- `SUCCESS` Verification + Fact/Stage progress + Commit Event 原子提交；
- Checkpoint + `CheckpointCreated` 原子提交；
- 相同 Task 的 sequence 在并发写入下仍严格递增；
- crash 后不会出现“Snapshot 已推进但 Event 缺失”或反向不一致。

## 19. 保留与隐私边界

必须持久化：

- Task、Stage、Constraint；
- 关键 Event；
- Action/Execution/Verification 元数据；
- 已验证 Fact；
- Checkpoint 元数据；
- ClientMessage 关联与幂等记录。

按配置保留：

- Screenshot；
- UI Tree；
- 模型原始响应；
- ADB 详细日志。

禁止作为产品数据持久化：

- 模型长思考；
- 无限 Token 流；
- 前端推断的第二套 Task 状态；
- 自动形成的长期用户记忆。

## 20. 状态

`DESIGN FROZEN — DATABASE NOT CREATED`

