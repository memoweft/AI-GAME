# 02 系统架构

## 1. 架构目标

系统架构只服务于一个闭环：多个聊天入口通过统一 Gateway 控制同一个保存任务事实的 Runtime；Runtime 调用角色化模型和 Android 设备层，基于观察执行并验证动作。

```text
                 User
                   │
       ┌───────────┴────────────┐
       │                        │
   Web Chat                Hermes / WeChat
       │                        │
       └───────────┬────────────┘
                   │
             Runtime Gateway
                   │
                   ▼
              Task Runtime
                   │
          ┌────────┼────────┐
          │        │        │
       Planner  Operator  Language
          │        │        │
        Router   Router   Router
          │        │        │
          └────────┴────────┘
                   │
                   ▼
             Device Layer
          ┌────────┼────────┐
          │        │        │
      Screenshot UI Tree Device State
                   │
                   ▼
            Action Executor
                   │
                  ADB
                   │
                Android
```

## 2. 组件职责

### 2.1 Client

Web Chat、Hermes 和微信适配层负责收发用户消息、显示任务状态和转交控制意图。Client 不持有 Runtime 真相，不直接调用 ADB，也不自行推进 Stage。

### 2.2 Runtime Gateway

Gateway 是所有外部入口的统一协议边界，负责：

- 接收用户消息并关联 Task；
- 创建、查询和控制 Task；
- 对外发布统一事件与状态；
- 处理 Client 身份、会话关联、幂等键和传输层错误；
- 屏蔽 Runtime 内部对象与设备层细节。

### 2.3 Task Runtime

Runtime 是事实和执行顺序的权威来源，负责：

- 保存 Goal、Constraints、Stage、Facts、状态和 Checkpoint；
- 调度 Planner、Operator、Language；
- 驱动 Observe、Decide、Act、Verify、Commit 循环；
- 记录关键事件；
- 处理用户干预、失败、重试、恢复和停止。

### 2.4 Model Roles 与 Router

系统定义 Planner、Operator、Language 三种逻辑角色。Router 为每次角色调用选择具体模型实现，可选择本地或云端，但不得把供应商或模型名渗透到核心 Runtime 契约。

### 2.5 Android Device Layer

设备层把 Android 与 ADB 能力封装为稳定接口，提供 Observation 和受控 Action。上层不拼接散落的 ADB 命令，也不依赖某个设备分辨率的固定坐标流程。

### 2.6 Event Store 与 Checkpoint

Event Store 保存已经发生的重要任务事实变化；Checkpoint 保存长任务恢复所需的紧凑快照。两者不承担模型思考归档。

## 3. 依赖方向

依赖方向必须保持单向：

```text
Client → Gateway → Task Runtime → Role Ports / Device Ports
                                   ↓
                         Model Adapters / ADB Adapter
```

具体模型适配器和 ADB 实现依赖 Runtime 定义的端口，而不是 Runtime 依赖具体供应商 SDK 或命令细节。

## 4. 控制流与事实流

一次典型执行：

1. Client 把用户消息发送到 Gateway。
2. Gateway 创建 Task 或把消息附加到 Active Task。
3. Runtime 解析控制意图，更新 Goal 或 Constraints。
4. Planner 在需要时给出当前 Stage。
5. Device Layer 获取 Observation。
6. Operator 根据 Stage 与 Observation 提出一个 Action 及 Expected Outcome。
7. Action Executor 执行动作。
8. Device Layer 获取新的 Observation。
9. Runtime 的 Verify 步骤返回 `SUCCESS / FAIL / UNCERTAIN`。
10. Runtime 仅在 `SUCCESS` 时 Commit 进度并记录事实。
11. Stage 完成时重新调用 Planner；Task 完成时由 Language 生成用户反馈。
12. Gateway 把状态和事件同步给 Client。

## 5. 进程与部署边界

v0.1 不强制规定每个组件必须独立进程。实现可以在一个后端服务中使用清晰模块边界，但逻辑责任不得混合。

尤其不能因为早期共进程，就让：

- Web 前端直接访问 ADB；
- Hermes 维护另一套任务状态；
- 模型适配器直接修改 Task；
- Action Executor 自行判定 Stage 完成；
- 日志被当作 Event Store。

## 6. 明确不引入的架构

本基线不引入任务 DAG、通用工作流引擎、Agent Swarm、多个自治 Planner、App 专用插件体系、复杂策略引擎或跨设备调度器。出现相关需求时必须作为新架构决策处理，而不是在实现中提前铺设。
