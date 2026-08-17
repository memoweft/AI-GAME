# Soul Mobile Agent Workbench 文档索引

> 版本：v0.1 设计基线  
> 状态：已冻结的实现输入  
> 适用范围：Soul Mobile Agent Workbench / Mobile Agent Runtime MVP

## 1. 这套文档解决什么问题

本目录把已经确认的产品与架构决定整理为可直接进入工程仓库的设计基线。它不继续扩张架构，也不提前设计 MVP 之外的系统。

Soul Mobile 的核心不是“让 AI 自动点击手机”，而是：

> 用户通过自然语言持续控制一个能够观察、操作、验证 Android 环境状态的 Task Runtime。

产品的主界面是聊天。Web、Hermes、微信等入口都是 Runtime Gateway 的 Client，共用同一个 Task Runtime。工程信息默认隐藏，只在需要时展开查看。

## 2. 已冻结的核心决定

1. 模型负责判断，Runtime 负责事实。
2. Runtime 保存目标、状态、观察、动作结果、事实、事件和 Checkpoint；不保存模型的冗长思考过程。
3. 任务只使用 `Goal → Stage → Action` 三层，不引入任务 DAG、Agent Swarm 或企业工作流引擎。
4. 核心循环为 `PLAN → OBSERVE → DECIDE → ACT → VERIFY → COMMIT`；高频循环为 `OBSERVE → DECIDE → ACT → VERIFY`。
5. Planner、Operator、Language 是逻辑角色，不绑定具体模型或供应商；Router 负责本地/云端与具体实现选择。
6. 动作已执行不等于目标已成功。Verify 独立返回 `SUCCESS / FAIL / UNCERTAIN`，只有 `SUCCESS` 可以 Commit。
7. 所有客户端都通过 Runtime Gateway 进入同一个 Task Runtime；客户端不能绕过 Gateway 直接操作 ADB。
8. 长任务依赖任务事实、事件和 Checkpoint 恢复，不依赖无限增长的模型 Context。
9. 用户可以通过后续自然语言消息暂停、继续、取消、接管或修改当前任务的目标与约束。
10. MVP 首个正式任务是：自然语言要求 Android 打开设置、进入电池页面、读取电池信息、反馈结果并回到桌面；不得使用为该任务预写的固定点击脚本。
11. 用户决定目标、边界、预期效果和最终取舍；ChatGPT/设计者决定技术路线、施工拆分和验证方法；Codex 只在施工单范围内实现与验证。
12. Codex 每完成一个可验证成果必须停止；如果出现新的架构决策、公共契约变化或范围冲突，必须提前停止。

## 3. 文档地图

| 文档 | 内容 | 首次阅读建议 |
|---|---|---|
| [01_PRODUCT_VISION](./01_PRODUCT_VISION.md) | 产品目标、体验原则、价值与边界 | 产品、设计、工程必读 |
| [02_SYSTEM_ARCHITECTURE](./02_SYSTEM_ARCHITECTURE.md) | 系统组件、职责和依赖方向 | 工程必读 |
| [03_TASK_RUNTIME](./03_TASK_RUNTIME.md) | Runtime 循环、Stage 和调度规则 | Runtime 开发必读 |
| [04_TASK_STATE_AND_EVENTS](./04_TASK_STATE_AND_EVENTS.md) | 状态机、事实、事件和投影 | 后端开发必读 |
| [05_MODEL_ROLES_AND_ROUTING](./05_MODEL_ROLES_AND_ROUTING.md) | Planner / Operator / Language 与 Router | 模型接入必读 |
| [06_ANDROID_ADB_DEVICE_LAYER](./06_ANDROID_ADB_DEVICE_LAYER.md) | Android 设备、ADB、观察与动作边界 | 设备层开发必读 |
| [07_OBSERVATION_ACTION_VERIFY](./07_OBSERVATION_ACTION_VERIFY.md) | 观察、动作、验证与 Commit 语义 | 闭环开发必读 |
| [08_RECOVERY_CHECKPOINT_LONG_TASKS](./08_RECOVERY_CHECKPOINT_LONG_TASKS.md) | 重试、卡住、升级、Checkpoint 和恢复 | 稳定性开发必读 |
| [09_RUNTIME_GATEWAY_API](./09_RUNTIME_GATEWAY_API.md) | Gateway 责任、会话与实时事件 | API 开发必读 |
| [10_FRONTEND_CHAT_WORKBENCH](./10_FRONTEND_CHAT_WORKBENCH.md) | 聊天主界面与展开式工作台 | 前端开发必读 |
| [11_HERMES_WECHAT_INTEGRATION](./11_HERMES_WECHAT_INTEGRATION.md) | Hermes / 微信适配边界与消息流 | 集成开发必读 |
| [12_SKILL_AND_MEMORY_BOUNDARIES](./12_SKILL_AND_MEMORY_BOUNDARIES.md) | Skill、任务事实、长期记忆的边界 | Runtime/模型开发必读 |
| [13_SECURITY_AND_USER_CONTROL_BOUNDARY](./13_SECURITY_AND_USER_CONTROL_BOUNDARY.md) | 控制点、可配置边界与用户控制 | 全员必读 |
| [14_MVP_SCOPE_AND_ACCEPTANCE](./14_MVP_SCOPE_AND_ACCEPTANCE.md) | MVP 范围、首个任务和验收证据 | 交付必读 |
| [15_IMPLEMENTATION_ROADMAP](./15_IMPLEMENTATION_ROADMAP.md) | 分阶段施工路线与停止点 | 项目执行必读 |
| [16_CODEX_EXECUTION_PROTOCOL](./16_CODEX_EXECUTION_PROTOCOL.md) | 决策权、施工单、STOP 协议 | 所有 Codex 任务必读 |
| [17_DATA_MODELS](./17_DATA_MODELS.md) | 核心实体与字段草案 | 后端开发参考 |
| [18_API_CONTRACT_DRAFT](./18_API_CONTRACT_DRAFT.md) | HTTP / SSE 契约草案 | 前后端联调参考 |
| [19_TEST_STRATEGY](./19_TEST_STRATEGY.md) | 单元、集成、真机与验收测试 | 测试与交付参考 |
| [20_OPEN_QUESTIONS_AND_NON_GOALS](./20_OPEN_QUESTIONS_AND_NON_GOALS.md) | 待决定项和明确非目标 | 变更控制必读 |

## 4. 建议阅读路径

产品与体验：`01 → 10 → 14 → 20`。

Runtime 实现：`02 → 03 → 04 → 07 → 08 → 17 → 19`。

设备与模型：`05 → 06 → 07 → 14 → 19`。

客户端与集成：`09 → 10 → 11 → 18`。

工程执行：`14 → 15 → 16 → 20`。

## 5. 术语约定

- **Task Runtime**：保存任务事实并驱动任务执行循环的核心运行时。
- **Runtime Gateway**：Web、Hermes、微信等客户端访问 Runtime 的唯一统一入口。
- **Goal**：用户最终希望达成的结果。
- **Stage**：由 Planner 给出的、可验证的当前阶段目标。
- **Action**：Operator 针对当前 Observation 提出的一个设备操作。
- **Observation**：截图、UI Tree 和设备状态组成的事实快照。
- **Verify**：动作之后基于新 Observation 判断预期状态是否达成。
- **Commit**：只有 Verify 成功后，才把动作结果记为已确认进度。
- **Fact**：Runtime 可引用的结构化任务事实，不等同于模型推理。
- **Checkpoint**：可用于恢复长任务的紧凑事实快照。
- **Client**：通过 Gateway 与 Runtime 交互的 Web、Hermes、微信或未来入口。
- **Router**：按角色、能力和运行条件选择模型实现的路由层。

## 6. 文档变更原则

以下变更不应由实现过程顺手决定：新增核心抽象、改变 `Goal → Stage → Action` 层级、改变 Gateway 唯一入口、让客户端直接操作设备、改变 Verify/Commit 语义、增加新的安全强制策略、把具体模型写入核心契约。遇到这些问题，应按 [16_CODEX_EXECUTION_PROTOCOL](./16_CODEX_EXECUTION_PROTOCOL.md) 提前停止并交回设计决策。

