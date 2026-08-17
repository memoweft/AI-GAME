# 18 API 契约草案

## 1. 状态

本文是 v0.1 的联调草案，目标是冻结资源语义和最小消息形态，不替实现选择 Web 框架。所有路径以 `/api/v1` 为前缀，JSON 使用 UTF-8。

## 2. 通用请求头

```http
Content-Type: application/json
Idempotency-Key: <opaque-key>   # 创建或控制类请求必需
X-Client-Id: <client-id>
```

具体认证头由部署决定，不在本草案写死。

## 3. 设备

### `GET /api/v1/devices`

返回可用于创建 Task 的设备摘要。

```json
{
  "items": [
    {
      "id": "device_01",
      "connection_state": "connected",
      "screen_size": [1080, 2400],
      "orientation": "portrait",
      "foreground_app": "com.android.launcher"
    }
  ]
}
```

## 4. 创建 Task

### `POST /api/v1/tasks`

```json
{
  "goal": "打开设置，查看当前电池信息，告诉我结果后回到桌面",
  "device_id": "device_01",
  "conversation_id": "conv_01",
  "message_id": "msg_01"
}
```

响应 `201 Created`：

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

相同幂等键重试返回同一 Task。

## 5. 查询 Task

### `GET /api/v1/tasks/{task_id}`

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
    "completed_stages": [
      {"id": "stage_01", "objective": "打开系统设置"}
    ],
    "last_observation_id": "obs_08",
    "last_event_sequence": 24,
    "updated_at": "2026-08-10T00:00:00Z"
  }
}
```

## 6. 发送用户消息

### `POST /api/v1/tasks/{task_id}/messages`

```json
{
  "message_id": "msg_02",
  "conversation_id": "conv_01",
  "text": "不用看耗电排行，只反馈当前页面能确认的信息"
}
```

响应 `202 Accepted`：

```json
{
  "accepted": true,
  "task_id": "task_01",
  "message_id": "msg_02",
  "event_sequence": 25
}
```

Runtime 后续通过事件说明该消息被解释为 Goal Change、Constraint Change、Additional Information 或控制命令。

## 7. 通过会话发送消息

### `POST /api/v1/conversations/{conversation_id}/messages`

用于 Hermes 等只持有会话关联的 Client。

```json
{
  "message_id": "wechat_msg_92",
  "text": "先停一下，我自己操作",
  "device_id": "device_01"
}
```

Gateway 返回关联或新建的 `task_id`。如果同一会话存在多个无法判定的 Active Task，应返回明确冲突，而不是任意选择。

## 8. 控制 Task

### `POST /api/v1/tasks/{task_id}/controls`

```json
{
  "command": "pause",
  "reason": "user_requested"
}
```

`command` 取值：

```text
pause
resume
cancel
takeover
```

响应：

```json
{
  "accepted": true,
  "task_id": "task_01",
  "command": "pause",
  "status": "PAUSED",
  "event_sequence": 27
}
```

`resume` 被接受后，Runtime 必须先重新 Observation；响应成功不表示已经继续产生设备动作。

## 9. 事件列表

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

## 10. SSE 实时流

### `GET /api/v1/tasks/{task_id}/events/stream?after_sequence=24`

```text
id: 25
event: runtime_event
data: {"sequence":25,"type":"ConstraintAdded","payload":{...}}

id: 26
event: runtime_event
data: {"sequence":26,"type":"ObservationReceived","payload":{...}}
```

Client 断线后用最后 sequence 续传，并在需要时重新获取 Task Snapshot。

## 11. Observation 预览

### `GET /api/v1/tasks/{task_id}/observations/{observation_id}`

返回元数据和一个受控预览引用，不默认内联原始大图或完整 UI Tree。

```json
{
  "observation": {
    "id": "obs_08",
    "captured_at": "...",
    "screenshot_url": "/api/v1/artifacts/art_08",
    "width": 1080,
    "height": 2400,
    "foreground_app": "com.android.settings",
    "ui_tree_available": true
  }
}
```

## 12. 错误响应

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

建议最小错误码：

| code | 场景 |
|---|---|
| `VALIDATION_ERROR` | 请求字段不合法 |
| `TASK_NOT_FOUND` | Task 不存在 |
| `TASK_NOT_ACTIVE` | 终态或当前状态不允许操作 |
| `DEVICE_NOT_FOUND` | 设备不存在 |
| `DEVICE_NOT_AVAILABLE` | 设备断连、未授权或被占用 |
| `CONVERSATION_CONFLICT` | 会话关联无法唯一确定 |
| `IDEMPOTENCY_CONFLICT` | 相同键对应不同请求内容 |
| `EVENT_CURSOR_INVALID` | 事件游标非法或已不可用 |
| `INTERNAL_ERROR` | 未分类内部错误 |

## 13. 对外事件投影

Gateway 可以在底层 Runtime Event 之外提供面向聊天的投影事件：

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

投影不能篡改底层状态；客户端调试详情仍可按权限读取原始结构化事件。

## 14. 契约未定项

认证方式、Artifact URL 的授权机制、分页上限、保留期限和具体错误 HTTP 状态仍需结合部署环境决定，列入 [20_OPEN_QUESTIONS_AND_NON_GOALS](./20_OPEN_QUESTIONS_AND_NON_GOALS.md)。

