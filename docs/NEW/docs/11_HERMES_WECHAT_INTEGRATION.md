# 11 Hermes / 微信集成

## 1. 集成定位

Hermes 和微信不是另一套 Mobile Agent，也不直接控制 Android。它们是 Runtime Gateway 的 Client。

```text
用户微信消息
    ↓
微信接入能力
    ↓
Hermes
    ↓
Runtime Gateway
    ↓
Task Runtime
    ↓
Android Device Layer
```

Web 工作台关闭时，后端 Runtime 仍可继续运行；Hermes 只负责传递消息、接收状态并回复用户。

## 2. Hermes 的职责

- 接收微信侧的用户消息与会话标识；
- 转换为 Gateway 的标准 Client Message；
- 携带稳定的 `client_id`、`conversation_id`、`message_id` 和幂等键；
- 接收 Runtime 的用户可读进度、等待请求和最终结果；
- 将这些消息回复到正确的微信会话；
- 保留最少的 Task 关联缓存，必要时以 Gateway 为准恢复。

## 3. Hermes 不负责

- 不解析屏幕或规划 Stage；
- 不生成 Android 点击坐标；
- 不维护独立的 Task 状态机；
- 不因为微信消息延迟而重发不确定的控制操作；
- 不绕过 Gateway 调用 ADB；
- 不把微信会话记录当作 Runtime Checkpoint。

## 4. 消息关联

每条入站消息转换为：

```json
{
  "client": "hermes-wechat",
  "conversation_id": "opaque-conversation-id",
  "message_id": "opaque-message-id",
  "text": "打开设置看看电池信息，然后回桌面",
  "sent_at": "..."
}
```

Gateway 根据 `conversation_id` 找到 Active Task。若不存在，创建新 Task；若存在，把消息作为当前 Task 的控制输入。Gateway 返回明确的 `task_id`，Hermes 不自行猜测关联。

## 5. 典型流程

### 5.1 创建任务

```text
用户：打开设置看看电池信息，然后回桌面
→ Hermes 转发消息
→ Gateway 创建 Task
→ Runtime 开始规划与执行
→ Hermes 回复“已开始，正在打开设置”
```

### 5.2 中途修正

```text
用户：不用看耗电排行，只告诉我当前能看到的电量信息
→ Hermes 发送到同一 task_id
→ Runtime 记录 Constraint Change
→ 后续 Stage 按新约束执行
```

### 5.3 暂停与继续

```text
用户：先停一下，我自己操作
→ Runtime PAUSED
→ 自动 Action 停止
→ 用户：好了，继续
→ Runtime 获取新 Observation 后继续
```

## 6. 出站消息类型

Hermes 至少处理：

- `progress`：有意义的阶段变化，不转发每个点击；
- `waiting_for_user`：需要用户输入或处理；
- `paused` / `resumed`；
- `completed`：已验证的最终结果；
- `failed`：明确失败和可行下一步；
- `cancelled`。

原始 Runtime Event、模型调用和 ADB 日志不应默认刷入微信聊天。

## 7. 幂等与重试

- 入站微信 `message_id` 映射为 Gateway 幂等键；
- 网络失败时可以重试发送同一请求，但不能生成新的语义请求；
- 如果 Hermes 不确定消息是否已经受理，应先按幂等键查询或重试原请求；
- 出站回复应记录已发送状态，避免任务事件重放造成重复刷屏。

## 8. 状态一致性

Runtime Gateway 是 Task 状态权威。Hermes 重启后，通过 `conversation_id` 查询 Active Task 和事件 sequence 恢复，不依赖本地内存中的“上次进行到哪里”。

Web 和微信可能同时查看或控制一个 Task。两端看到的状态都由同一事件流投影，控制消息按 Gateway 接收顺序处理。

## 9. MVP 接入边界

MVP 只要求契约与适配边界成立。微信具体接入方式、部署网络、账号运维和消息平台能力属于部署问题；如果这些条件暂未具备，可以先用 Hermes Adapter 的契约测试或模拟 Client 验证，但不能把模拟通过描述为真实微信端到端通过。

## 10. 验收

- Hermes 能通过 Gateway 创建或继续 Task；
- 同一微信会话的后续消息能控制正确的 Active Task；
- Runtime 进度和最终结果可以映射为微信可读消息；
- Web 与 Hermes 不产生两套 Task；
- Hermes 无法直接访问设备层；
- 重试不会重复创建任务或重复应用同一用户控制。

