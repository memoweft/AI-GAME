# 09 Runtime Gateway API

## 1. Gateway 定位

Runtime Gateway 是 Web、Hermes、微信及未来 Client 与 Task Runtime 交互的唯一入口。它统一消息、任务控制、状态查询和实时事件，不复制 Runtime 业务逻辑。

```text
Web Chat ───────┐
Hermes / WeChat ├── Runtime Gateway ── Task Runtime ── Android
Future Client ──┘
```

## 2. Gateway 责任

- 把用户消息关联到新 Task 或当前 Active Task；
- 提供 Task 创建、读取、暂停、继续、取消和接管接口；
- 将 Runtime Event 转换为稳定的实时流；
- 处理 Client、conversation 与 task 的关联；
- 为写请求提供幂等支持；
- 返回结构化错误；
- 保护内部 Runtime 与设备层接口不被直接暴露。

## 3. Gateway 不负责

- 不调用 Planner 决定 Stage；
- 不直接执行 ADB Action；
- 不自行解释屏幕是否成功；
- 不维护第二套 Task 状态机；
- 不为 Web 和 Hermes 实现不同的任务语义；
- 不把聊天平台的消息格式带入 Runtime 核心模型。

## 4. 核心资源

v0.1 只需要以下概念资源：

- `devices`：可用 Android 设备及连接状态；
- `tasks`：任务创建、状态和控制；
- `messages`：发送到某个 Task 的用户输入；
- `events`：某个 Task 的增量事件流；
- `observations`：受控读取的观察引用或预览。

具体路径草案见 [18_API_CONTRACT_DRAFT](./18_API_CONTRACT_DRAFT.md)。

## 5. Task 关联

Client 发送消息时可以：

1. 明确提供 `task_id`，控制已有 Task；
2. 提供 `conversation_id`，由 Gateway 关联该会话的 Active Task；
3. 没有可关联 Task 时，创建新 Task。

Gateway 必须把最终关联结果返回给 Client。微信端不能仅凭“最近一条消息”猜测 Task。

## 6. 实时事件

MVP 推荐使用 SSE 向 Web 工作台推送增量事件；Hermes 可以使用相同事件语义，通过轮询或服务端消费方式获取。传输方式可以不同，事件模型必须相同。

Client 通过 `after_sequence` 断线续传，避免因为重连丢失进度或重复展示全部历史。

## 7. 幂等性

创建 Task、发送用户消息和控制命令都应接受 `Idempotency-Key`。同一 Client 重试相同请求时，Gateway 返回原结果，不重复创建 Task、不重复添加约束，也不重复取消。

## 8. 错误模型

Gateway 返回统一错误：

```json
{
  "error": {
    "code": "TASK_NOT_ACTIVE",
    "message": "当前任务不能接受该操作",
    "retryable": false,
    "details": {}
  }
}
```

HTTP 状态表达传输或资源层语义；`error.code` 表达稳定的业务错误。模型供应商原始错误和 ADB 原始输出不直接暴露给普通 Client。

## 9. 状态与消息展示

Gateway 应提供两个层级：

- 面向主聊天的简洁投影：当前阶段、状态、用户可读进度；
- 面向展开详情的工程投影：最近动作、Verify、角色 Binding、事件和 Observation 预览。

二者都来源于同一 Runtime 事件与 Snapshot。

## 10. 版本策略

v0.1 API 使用显式版本前缀，例如 `/api/v1`。Draft 阶段允许字段细化，但一旦前后端开始联调，破坏性变化必须按新设计决策处理，不能由实现方静默修改。

