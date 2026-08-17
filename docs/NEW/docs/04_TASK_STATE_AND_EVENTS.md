# 04 任务状态与事件

## 1. Task 状态

v0.1 使用以下状态：

| 状态 | 含义 |
|---|---|
| `CREATED` | Task 已创建，尚未形成当前 Stage |
| `PLANNING` | 正在请求或应用 Planner 结果 |
| `RUNNING` | 正在执行当前 Stage 的观察与动作循环 |
| `WAITING` | 等待用户、设备或明确的外部条件 |
| `PAUSED` | 用户或 Runtime 已暂停，不继续发出设备动作 |
| `STUCK` | 多轮无进度，常规重试已不能合理继续 |
| `COMPLETED` | Goal 的完成条件已验证，收尾动作也已完成 |
| `FAILED` | Task 已终止且未完成，存在明确失败原因 |
| `CANCELLED` | 用户取消 Task |

状态是 Runtime 投影，不由模型直接写入。

## 2. 允许的主要状态转换

```text
CREATED → PLANNING → RUNNING
RUNNING → PLANNING          Stage 完成、失败或需要重规划
RUNNING → WAITING           等待用户或设备
RUNNING → PAUSED            用户暂停或手动接管
RUNNING → STUCK             连续无意义循环
WAITING → RUNNING           等待条件满足
PAUSED → RUNNING            用户明确继续且重新观察
STUCK → PLANNING            恢复或升级后重规划
任意非终态 → CANCELLED      用户取消
RUNNING/PLANNING → FAILED    不可恢复失败
RUNNING → COMPLETED          Goal 与收尾均验证成功
```

终态是 `COMPLETED / FAILED / CANCELLED`。终态 Task 不再接受设备动作；如需继续，应由上层明确创建新 Task 或采用未来定义的重开语义。

## 3. Task Snapshot 与事实

Task Snapshot 是由事件投影得到的当前事实视图，至少包括：

```text
id
goal
status
constraints[]
current_stage
completed_stages[]
facts[]
device_id
current_app
last_observation_ref
recent_actions[]
failure_state
checkpoint_ref
active_role_bindings
created_at
updated_at
```

事实必须注明来源，例如：用户明确提供、Observation 直接读取、Verify 确认、Planner 建议但尚未验证。未验证的模型判断不能与已验证事实混在一起。

## 4. 核心事件

| 事件 | 触发点 | 关键载荷 |
|---|---|---|
| `TaskCreated` | Gateway 创建 Task | goal、source、device_id |
| `GoalUpdated` | 用户改变目标 | old_goal、new_goal、message_id |
| `ConstraintAdded` / `ConstraintRemoved` | 用户调整约束 | constraint、source |
| `StageStarted` | Planner 结果被 Runtime 接受 | stage、completion_criteria |
| `ObservationReceived` | 设备快照完成 | observation_ref、device_state |
| `ActionProposed` | Operator 返回候选动作 | action、expected_outcome、role_call_id |
| `ActionExecuted` | 设备层执行完成 | action_id、transport_result |
| `ActionVerified` | Verify 完成 | verdict、evidence_refs、reason |
| `FactAdded` | 新事实被 Commit | fact、confidence、source_refs |
| `StageCompleted` | Stage 完成条件已验证 | stage_id、evidence_refs |
| `ModelEscalated` | Router 切换实现级别 | role、from_binding、to_binding、reason |
| `CheckpointCreated` | 生成恢复点 | checkpoint_ref、reason |
| `UserIntervened` | 用户暂停、修正或接管 | intervention_type、message_id |
| `TaskPaused` / `TaskResumed` | 状态改变 | reason、actor |
| `TaskCompleted` | Goal 与收尾完成 | result、evidence_refs |
| `TaskFailed` | 不可恢复失败 | failure_code、summary |
| `TaskCancelled` | 用户取消 | actor、reason |

## 5. 事件设计规则

1. 事件描述已经发生的事实，使用过去时语义。
2. 每个事件有唯一 `event_id`、`task_id`、单调递增 `sequence` 和时间戳。
3. 事件载荷使用结构化字段；面向用户的文字是投影，不是唯一事实来源。
4. 原始截图、UI Tree 和较大模型响应使用引用，不直接塞入事件主体。
5. Event Store 不是日志文件。调试日志可以丢失，任务事件不能悄悄丢失或重排。
6. 同一个幂等请求不能重复产生语义相同的控制事件。

## 6. `UNCERTAIN` 的状态处理

`UNCERTAIN` 是 Verify 结果，不是 Task 顶层状态。Runtime 通常保持 `RUNNING`，先重新观察或调整验证方法；如果多次不确定且无法形成新证据，再进入 `STUCK` 或 `WAITING`，并记录原因。

## 7. 用户消息与事件关系

每条用户消息都保留 `message_id` 和 Client 来源。消息被 Runtime 解释后，应产生相应事件，例如 `ConstraintAdded`、`TaskPaused` 或 `GoalUpdated`。这样 Web 与微信可以看到同一事实，而不是各自维护一套解释。

