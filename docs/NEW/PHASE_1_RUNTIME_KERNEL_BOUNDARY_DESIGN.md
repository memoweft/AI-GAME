# Phase 1 — Runtime Kernel Boundary Design

> 状态：FROZEN DESIGN  
> 日期：2026-08-10  
> 实现状态：NOT STARTED  
> 依据：新 v0.1 设计基线、Phase 0 现状地图与已确认的方案 B

## 1. 目的

在现有 AI-GAME FastAPI 后端内建立一个独立的 Soul Mobile Agent Runtime v0.1 Kernel 边界。本文只冻结目录、职责、依赖方向和新旧系统关系，不创建包、不写实现代码。

新 Kernel 的目标控制语义是：

```text
Goal
  ↓
Current Stage
  ↓
Single Action
  ↓
Verify
  ↓
Commit
```

它不继承旧 MobileTask 的“完整 TaskPlan + 多个 Subgoal + Reflection + SkillMemory”控制哲学。

## 2. 已冻结决策

1. 采用方案 B：在现有工程内建立独立 v0.1 Runtime Kernel 和独立 Store。
2. 不重新建立项目，也不原地改名或迁移旧 `mobile_agent` 包。
3. New Runtime 是未来唯一 Mobile Runtime 核心。
4. Legacy 只能通过明确 Adapter 或只读 Archive 边界存在；激活新 Kernel 后不得直接控制 Android。
5. Root `F:\AI-GAME\runtime` 已是运行数据目录，不作为 Python 源码目录。
6. 新 Python 包的目标位置冻结为：

```text
apps/console/backend/ai_game_console/runtime_kernel/
```

7. 新持久化使用独立 `runtime.db`；旧 `mobile-tasks.db` 不直接迁移。
8. Soul 位于 Runtime Gateway 之上，提供人格、偏好与上层 Agent 能力；Mobile Runtime 是设备执行身体。

## 3. 目标组件关系

```text
Web / Hermes / WeChat / Soul
              │
              ▼
       Runtime Gateway
              │
              ▼
        Runtime Kernel
              │
     ┌────────┼─────────┐
     │        │         │
   Planner  Operator  Language
     │        │         │
     └────────┴─────────┘
              │
              ▼
       Device Port Boundary
              │
              ▼
       Action Executor
              │
              ▼
   DeviceExecutionLease / ADB
              │
              ▼
           Android
```

Legacy 关系：

```text
Legacy Client / Frozen History
              │
              ▼
       Legacy Adapter
              │
              ├── read-only legacy archive
              └── explicitly supported intent translation
                           │
                           ▼
                    Runtime Gateway
                           │
                           ▼
                    Runtime Kernel
```

Legacy Adapter 不是第二个调度器，不能持有 Device Port、ADB Adapter 或 DeviceExecutionLease。

## 4. 目标目录

```text
apps/console/backend/ai_game_console/
│
├── runtime_kernel/
│   ├── __init__.py
│   ├── kernel.py
│   ├── ports.py
│   │
│   ├── task/
│   │   ├── domain.py
│   │   └── lifecycle.py
│   │
│   ├── stage/
│   │   ├── domain.py
│   │   └── service.py
│   │
│   ├── event/
│   │   ├── domain.py
│   │   ├── store.py
│   │   └── projection.py
│   │
│   ├── observation/
│   │   ├── domain.py
│   │   └── service.py
│   │
│   ├── action/
│   │   ├── domain.py
│   │   ├── validation.py
│   │   └── service.py
│   │
│   ├── verify/
│   │   ├── domain.py
│   │   └── service.py
│   │
│   ├── checkpoint/
│   │   ├── domain.py
│   │   └── service.py
│   │
│   ├── router/
│   │   ├── domain.py
│   │   └── service.py
│   │
│   └── context/
│       ├── domain.py
│       └── builder.py
│
├── runtime_gateway/
│   ├── service.py
│   ├── association.py
│   ├── idempotency.py
│   ├── projections.py
│   └── legacy_adapter.py
│
└── runtime_adapters/
    ├── sqlite_runtime_store.py
    ├── android_device_adapter.py
    ├── role_model_adapter.py
    └── artifact_store.py
```

这棵树表达目标边界，不授权一次性创建全部文件。后续施工单应按 Phase 1/2 等可验证成果最小落地。

## 5. 模块职责

### 5.1 `kernel.py`

负责：

- 接受 Gateway 已规范化的 Task 命令；
- 串行驱动 `PLAN → OBSERVE → DECIDE → ACT → VERIFY → COMMIT`；
- 根据事件与持久状态推进 Task；
- 在用户消息、设备异常和恢复边界处协调各模块；
- 保证同一设备只有一个 Active Task 执行循环。

不负责：

- HTTP、SSE 或微信消息格式；
- 拼接 ADB 命令；
- 绑定具体模型名称；
- 直接写 SQLite SQL；
- 生成 UI 文案；
- 保存模型长推理。

### 5.2 `ports.py`

集中声明 Kernel 依赖的稳定端口：

- `TaskStorePort`
- `EventStorePort`
- `ObservationProviderPort`
- `ActionExecutorPort`
- `RoleRouterPort`
- `CheckpointStorePort`
- `ArtifactStorePort`
- `DeviceLeasePort`

端口只使用 Runtime Kernel 的逻辑模型，不暴露 FastAPI、sqlite3、ADB subprocess 或供应商 SDK 类型。

### 5.3 `task/`

保存 Goal、Constraints、Task 状态、当前 Stage 引用和终态规则。Task 是事实聚合根，但历史事实来自 Event Store，较大对象通过引用关联。

禁止：

- 保存完整预生成动作清单；
- 让模型直接写 Task status；
- 把前端本地状态当作 Task 真相。

### 5.4 `stage/`

维护唯一 Current Stage、完成条件及 Stage 生命周期。Planner 每次只能提议一个当前 Stage；Stage 服务校验后才成为 Runtime 事实。

Stage 不拥有 Action 列表，不承担完整任务 DAG，也不变成旧 Subgoal 数组的重命名包装。

### 5.5 `event/`

定义追加式 Runtime Event、Task 内单调 sequence、事件持久化与 Snapshot 投影。

Event Store 保存已经发生的事实，不保存日志文本或模型思考。投影器必须能从事件恢复 Task Snapshot；调试日志不参与状态恢复。

### 5.6 `observation/`

把同一时间窗口的 Screenshot、UI Tree（可用时）和 Device State 组合为 Observation，并标记一致性风险。

该模块只获取事实，不判断 Stage 成功，也不决定下一步动作。

### 5.7 `action/`

接受 Operator 的一个结构化 Action Proposal，校验：

- Action 类型和参数；
- based-on Observation 是否仍为最新；
- 坐标与当前屏幕；
- Task、设备和 Lease 状态；
- Expected Outcome 是否存在。

校验通过后才调用 Action Executor。Transport accepted 只进入 ActionExecution 事实，不能直接推进 Stage。

### 5.8 `verify/`

基于动作前 Observation、Action、Expected Outcome、Transport Result、动作后 Observation 和 Stage 完成条件返回：

```text
SUCCESS | FAIL | UNCERTAIN
```

Verify 是 Runtime 能力，不新增第四个核心模型角色。实现可以是规则、角色辅助或组合，但所有辅助结果仍由 Runtime 校验并形成 Verification 事实。

只有 `SUCCESS` 可以进入 Commit。

### 5.9 `checkpoint/`

创建和恢复紧凑 Task Checkpoint。Checkpoint 只包含 Goal、约束、当前 Stage、已完成 Stage 摘要、已验证 Fact、设备摘要、失败摘要和最后 Event sequence。

它不复制完整聊天历史、旧截图、模型 Prompt 或思考过程。

### 5.10 `router/`

根据逻辑角色、能力、配置、服务可用性和升级级别选择 RoleBinding：

```text
Planner  → local or cloud
Operator → local（v0.1 默认）
Language → local or cloud
```

Kernel 只依赖 Planner、Operator、Language 接口；供应商、模型名和 endpoint 只存在于 Adapter 配置与调用元数据。

### 5.11 `context/`

为角色调用构造有界上下文：

```text
Goal
+ Active Constraints
+ Current Stage
+ Verified Facts
+ Latest Observation
+ bounded recent failures/actions
```

Context Builder 不从无限聊天历史恢复任务，不保存或重放模型思考，也不把未验证推断提升为 Fact。

### 5.12 `runtime_gateway/`

负责 HTTP/Client 边界、conversation/task 关联、幂等、SSE、错误映射及聊天/工程双层投影。Gateway 调用 Kernel 的公开应用服务，不访问 Kernel Store 表，也不操作 ADB。

### 5.13 `runtime_adapters/`

实现 ports 与现有基础设施的连接。Adapter 依赖 Kernel 端口，Kernel 不反向依赖现有具体实现。

可复用候选：

- `adb_executor.py`
- `discovery.py`
- `device_lease.py`
- `gui_owl_client.py`
- 现有 SQLite 事务与幂等实现方式

候选复用不等于直接 import 旧 MobileTask domain。

## 6. 依赖方向

唯一允许的主依赖方向：

```text
Client
  → Runtime Gateway
    → Runtime Kernel public service
      → Kernel domain/services
        → Kernel ports
          ← Infrastructure adapters
```

禁止：

- Kernel import FastAPI request/response schema；
- Kernel import `sqlite3.Row`；
- Kernel import具体 GUI-Owl/OpenAI/ADB client；
- Gateway 直接写 Runtime Store；
- Model Adapter 直接修改 Task；
- Legacy Runtime 直接调用 Device Adapter；
- Frontend 直接访问 ADB 或 Event Store。

## 7. Runtime 循环边界

```text
Gateway command accepted
        ↓
Kernel serial command queue
        ↓
PLAN（仅在需要时）
        ↓
OBSERVE（全新设备事实）
        ↓
DECIDE（一个 Action + Expected Outcome）
        ↓
ACT（结构校验 + lease + transport）
        ↓
OBSERVE（动作后全新设备事实）
        ↓
VERIFY（SUCCESS / FAIL / UNCERTAIN）
        ↓
COMMIT（仅 SUCCESS）
        ↓
event append + snapshot projection
```

用户 Pause、Cancel、Takeover、Goal/Constraint Change 和 shutdown 与设备下发 seam 共享串行栅栏。任何无法确认是否已下发的动作都先重新观察，不能盲目重放。

## 8. Soul 与 Runtime 的边界

```text
Soul
  ├── personality
  ├── user preference
  ├── optional task-level memory input
  └── language/agent behavior
          │
          ▼
   Runtime Gateway Client contract
          │
          ▼
   Mobile Runtime Kernel
          │
          ▼
       Android
```

Soul 不能：

- 直接调用 ADB；
- 持有另一套 Task 状态机；
- 把模型推断自动写为 Runtime verified Fact；
- 把 Task Fact 自动晋升为长期记忆；
- 绕过 Gateway 改变设备。

长期记忆仍不属于 v0.1 实现。本文中的 Soul personality/memory 只说明未来层次关系，不授权建设长期记忆系统。

## 9. 前端边界

Phase 1 不修改前端。冻结目标是：

- 默认主界面是 Chat；
- 展开详情包含 Current Task、Current Stage、Phone View、Actions、Events、Models 和 Runtime State；
- UI 状态只来自 Gateway Snapshot/Event；
- Web 与 Hermes/微信共享同一 Gateway 任务语义；
- 前端关闭不终止 Runtime。

## 10. 不包含

- Runtime 代码；
- SQLite schema 实际创建或迁移；
- FastAPI route 实际修改；
- Legacy cutover；
- 前端改造；
- 模型接入与 Prompt；
- Android 动作或真机测试；
- 长期记忆、完整 Skill、DAG、Agent Swarm、多设备调度。

## 11. 后续施工切分建议

本文不自动授权后续施工。建议最小顺序：

1. Runtime Kernel 的 Task/Event/Stage 纯 domain skeleton；
2. 独立 SQLite Store 与 projection；
3. Device ownership cutover seam；
4. Observation/Action ports；
5. Verify/Commit；
6. Role ports/Router；
7. Gateway；
8. Chat Workbench；
9. 真机电池任务。

每项都必须另有施工单并在成果后 STOP。

## 12. 状态

`DESIGN FROZEN — IMPLEMENTATION NOT STARTED`

