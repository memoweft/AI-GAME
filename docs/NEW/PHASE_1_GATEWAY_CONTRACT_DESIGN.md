# Phase 1 — Runtime Gateway Contract Design

> 状态：FROZEN DESIGN  
> 日期：2026-08-10  
> API 实现状态：NOT STARTED

## 1. 责任

Runtime Gateway 是 Web、Hermes、微信、Soul 和未来 Client 访问 New Runtime Kernel 的唯一协议入口。

负责：

- Client/conversation/message/task 关联；
- Task 创建、读取和控制；
- 用户消息输入；
- 统一错误与幂等；
- Snapshot 与聊天友好投影；
- 增量 Event 与 SSE 续传；
- Legacy Adapter 的受控入口。

不负责：

- 规划 Stage；
- 执行 ADB；
- 判断动作是否成功；
- 保存第二套 Task 状态；
- 暴露模型供应商或 ADB 原始输出；
- 让不同 Client 拥有不同任务语义。

## 2. 版本与现有路由冲突

目标 New Runtime 公共契约仍以 `/api/v1` 为版本前缀，核心资源语义采用新基线：

```text
/api/v1/devices
/api/v1/tasks
/api/v1/tasks/{task_id}
/api/v1/tasks/{task_id}/messages
/api/v1/tasks/{task_id}/controls
/api/v1/tasks/{task_id}/events
/api/v1/tasks/{task_id}/events/stream
/api/v1/tasks/{task_id}/observations/{observation_id}
/api/v1/conversations/{conversation_id}/messages
```

当前旧系统已经占用部分 `/api/v1/tasks` 路径。Phase 1 只冻结目标契约，不修改现有 route。实际启用必须是独立的 Gateway cutover 施工单，并与 Legacy Migration Strategy 同步完成，不能在同一路径上按请求体猜测新旧语义。

目标切换规则：

1. cutover 前，当前旧 route 行为保持不变；New Runtime route 不对外宣称可用。
2. cutover 时，旧 device work 先 drain，旧数据库冻结，旧写接口停止。
3. cutover 后，以上 canonical 路径只表达 New Runtime 契约。
4. 旧历史若继续通过 HTTP 查看，使用显式只读 legacy archive namespace；不得把旧 TaskPlan 投影伪装为新 Stage/Fact。

Legacy archive 的精确公开路径属于 cutover 施工单的兼容表面，不影响 New Runtime canonical contract；推荐方向是 `/api/v1/legacy/tasks`，且只能读取。

## 3. 通用请求头

```http
Content-Type: application/json
Idempotency-Key: <opaque-key>
X-Client-Id: <stable-client-id>
```

创建 Task、发送消息和控制命令必须有 `Idempotency-Key`。认证方式仍由部署决策决定，不能在 Phase 1 写死。

## 4. Device

### `GET /api/v1/devices`

返回可用于 New Runtime Task 的设备摘要：

```json
{
  "items": [
    {
      "id": "device_01",
      "connection_state": "connected",
      "screen_size": [1080, 2400],
      "orientation": "portrait",
      "foreground_app": "com.android.launcher",
      "availability": "available"
    }
  ]
}
```

设备被 Legacy 或其他未迁移 owner 控制时不得显示为可由 New Runtime 使用的 `available`。

## 5. 创建 Task

### `POST /api/v1/tasks`

```json
{
  "goal": "打开设置，查看当前电池信息，告诉我结果后回到桌面",
  "device_id": "device_01",
  "conversation_id": "conv_01",
  "message_id": "msg_01"
}
```

响应：

```json
{
  "task": {
    "id": "task_01",
    "goal": "打开设置，查看当前电池信息，告诉我结果后回到桌面",
    "status": "CREATED",
    "device_id": "device_01",
    "current_stage": null,
    "last_event_sequence": 1
  }
}
```

相同幂等键和 payload 返回同一 Task；相同键不同 payload 返回 `IDEMPOTENCY_CONFLICT`。

## 6. 查询 Task

### `GET /api/v1/tasks/{task_id}`

返回 Gateway Snapshot，而不是 Store row：

```json
{
  "task": {
    "id": "task_01",
    "goal": "...",
    "status": "RUNNING",
    "device_id": "device_01",
    "constraints": [],
    "current_stage": {
      "id": "stage_02",
      "objective": "进入电池相关设置页面",
      "completion_criteria": ["页面显示可识别的电池信息"]
    },
    "completed_stages": [],
    "verified_facts": [],
    "last_observation_id": "obs_08",
    "last_event_sequence": 24,
    "updated_at": "2026-08-10T00:00:00Z"
  }
}
```

主聊天投影可以隐藏工程字段，但必须来源于同一 Snapshot/Event。

## 7. 用户消息

### `POST /api/v1/tasks/{task_id}/messages`

```json
{
  "message_id": "msg_02",
  "conversation_id": "conv_01",
  "text": "不用看耗电排行，只反馈当前页面能确认的信息"
}
```

响应只表达受理：

```json
{
  "accepted": true,
  "task_id": "task_01",
  "message_id": "msg_02",
  "event_sequence": 25
}
```

Runtime 随后把消息分类为：

```text
Goal Change
Constraint Change
Additional Information
Pause
Resume
Cancel
Takeover
```

分类结果通过 Runtime Event 回显。Gateway 不在 HTTP Handler 内复制 Runtime 解释逻辑。

## 8. 会话入口

### `POST /api/v1/conversations/{conversation_id}/messages`

用于 Hermes、微信或只持有 conversation 的 Client：

```json
{
  "message_id": "client_msg_92",
  "text": "先停一下，我自己操作",
  "device_id": "device_01"
}
```

关联规则：

1. conversation 有唯一 Active Task：附加到该 Task。
2. 无 Active Task 且消息是新 Goal：创建 Task。
3. 存在多个无法唯一判断的 Active Task：返回 `CONVERSATION_CONFLICT`。
4. 不能仅凭最近消息或显示名称猜测 Task。
5. 返回最终 `task_id`。

## 9. 控制

### `POST /api/v1/tasks/{task_id}/controls`

```json
{
  "command": "pause",
  "reason": "user_requested"
}
```

`command`：

```text
pause
resume
cancel
takeover
```

响应示例：

```json
{
  "accepted": true,
  "task_id": "task_01",
  "command": "pause",
  "status": "PAUSED",
  "event_sequence": 27
}
```

语义：

- `accepted` 只表示控制命令已经持久化并进入 Kernel 顺序；
- `resume` 响应不表示设备动作已恢复，Runtime 必须先 Observe；
- `takeover` 使用 `PAUSED` Task 状态，并明确用户接管投影；
- `cancel` 进入终态 `CANCELLED`，不能映射成旧 `stopped` 后继续执行。

## 10. Event 列表

### `GET /api/v1/tasks/{task_id}/events?after_sequence=24&limit=100`

```json
{
  "items": [
    {
      "id": "evt_25",
      "task_id": "task_01",
      "sequence": 25,
      "type": "ConstraintAdded",
      "actor": "user",
      "payload": {"text": "不用看耗电排行"},
      "created_at": "2026-08-10T00:00:01Z"
    }
  ],
  "next_after_sequence": 25
}
```

sequence 是 Task 范围游标，不使用时间戳替代。分页结果严格升序且不重复。

## 11. SSE

### `GET /api/v1/tasks/{task_id}/events/stream?after_sequence=24`

```text
id: 25
event: runtime_event
data: {"sequence":25,"type":"ConstraintAdded","payload":{...}}

id: 26
event: runtime_event
data: {"sequence":26,"type":"ObservationReceived","payload":{...}}
```

约束：

- SSE 只读；
- 重连携带最后成功处理的 sequence；
- Client 重连后重新 GET Task Snapshot 校准；
- 慢 Client 不能阻塞 Kernel 执行；
- Event 投递失败不回滚已提交 Runtime Event；
- 原始截图和 UI Tree 不内联在 SSE；
- Gateway 可以发心跳，但心跳不是 Runtime Event，也不占 Task sequence。

## 12. Observation 预览

### `GET /api/v1/tasks/{task_id}/observations/{observation_id}`

```json
{
  "observation": {
    "id": "obs_08",
    "captured_at": "...",
    "screenshot_url": "/api/v1/artifacts/art_08",
    "width": 1080,
    "height": 2400,
    "foreground_app": "com.android.settings",
    "ui_tree_available": true,
    "consistency_status": "consistent"
  }
}
```

Artifact 授权和保留期限仍是部署待决定项。旧图必须显示采集时间与过期/断连状态。

## 13. 两层事件投影

### Runtime Event

用于恢复、审计和工程详情，保持结构化事实。

### Client Projection Event

```json
{
  "type": "task_progress",
  "task_id": "task_01",
  "sequence": 30,
  "status": "RUNNING",
  "stage": "读取当前页面可见的电池信息",
  "message": "已进入电池页面，正在读取信息。"
}
```

Projection 不能制造 Runtime 中不存在的 Stage、Fact 或完成状态。微信/Hermes 默认消费用户可读投影，不转发每次点击和 ADB 日志。

## 14. 错误模型

```json
{
  "error": {
    "code": "DEVICE_NOT_AVAILABLE",
    "message": "目标设备当前不可用",
    "retryable": true,
    "details": {"device_id": "device_01"}
  }
}
```

最小错误码：

| code | 场景 |
|---|---|
| `VALIDATION_ERROR` | 字段或 Action 不合法 |
| `TASK_NOT_FOUND` | Task 不存在 |
| `TASK_NOT_ACTIVE` | 当前状态不允许操作 |
| `DEVICE_NOT_FOUND` | 设备不存在 |
| `DEVICE_NOT_AVAILABLE` | 断连、未授权、被占用或 ownership 未切换 |
| `CONVERSATION_CONFLICT` | 会话无法唯一关联 |
| `IDEMPOTENCY_CONFLICT` | 相同键对应不同 payload |
| `EVENT_CURSOR_INVALID` | 游标非法或已不可用 |
| `LEGACY_TASK_WRITE_DISABLED` | cutover 后旧 Task write 被禁用 |
| `INTERNAL_ERROR` | 未分类内部错误 |

供应商原始错误、ADB stdout/stderr 和内部 traceback 不直接返回普通 Client。

## 15. Legacy Adapter 契约边界

Legacy Adapter 可以：

- 读取冻结的旧 Task history；
- 把明确支持的旧 Client 高层输入转换为 New Gateway command；
- 返回显式 deprecation/error；
- 保留原幂等键来源。

Legacy Adapter 不可以：

- 直接调用 Kernel Store；
- 直接获取 Device Lease；
- 把旧完整 TaskPlan 写成 New Stage 列表；
- 把旧 `uncertain/stopped` 静默映射成新 `COMPLETED/CANCELLED`；
- 在同一 `/api/v1/tasks` route 中按 payload 猜测新旧版本；
- 保持第二套 Active Task 状态机。

## 16. 安全与配置点

本文只冻结必要控制点，不增加新的产品安全策略。部署配置继续决定：

- Client 认证；
- Artifact 授权；
- 可用设备；
- 本地/云端模型；
- Observation 数据去向；
- 保留期限；
- retry/no-progress 阈值；
- Hermes/微信是否启用。

## 17. 前后端联调验收（未来）

后续 Gateway 实现至少要通过：

- Task create/get；
- message/control；
- conversation 唯一关联；
- 幂等 create/message/control；
- Event sequence 与分页；
- SSE 断线续传；
- Snapshot + Event 校准；
- Web/Hermes 共享语义；
- Client 无法访问 ADB；
- legacy write 不会绕过 Kernel。

这些是未来验收标准，不是 Phase 1 Runtime 证据。

## 18. 状态

`DESIGN FROZEN — API NOT MODIFIED`
