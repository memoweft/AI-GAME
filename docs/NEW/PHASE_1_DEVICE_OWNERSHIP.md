# Phase 1 — Device Ownership

> 状态：FROZEN DESIGN  
> 日期：2026-08-10  
> 实现状态：NOT STARTED

## 1. 决策

New Runtime Kernel 激活后，是 AI-GAME 内唯一有权为目标 Android 设备发出自动 Action 的核心。

```text
Runtime Kernel
      │
      ▼
Action Executor
      │
      ▼
DeviceExecutionLease
      │
      ▼
ADB Adapter
      │
      ▼
Android Device
```

`DeviceExecutionLease` 是执行权证明，不是 Task 规划器。Kernel 决定哪个 Task 可以申请执行；Action Executor 在每次下发前验证有效 Lease、Task 状态、设备标识和 Observation 版本。

## 2. 所有权不变量

1. 一个 `device_id` 同一时刻最多存在一个自动执行 Lease。
2. 一个 Lease 绑定唯一 Runtime Kernel、Task 和 Device Session。
3. Lease 不可由 Client、Legacy Runtime、Model Adapter 或前端获得。
4. Action 必须引用最新允许使用的 Observation。
5. Pause、Cancel、Takeover、终态或 Lease 失效后，不得产生新自动 Action。
6. Resume 后必须先获取全新 Observation；接管前的 Action Proposal 失效。
7. 设备断连、未授权或标识变化时 Lease 不授权继续动作。
8. Transport 状态不明确时不得重放；先 Observe，再决定是否提出新 Action。
9. Legacy 与 New Runtime 不得通过不同进程、不同 ADB executable 或外部 owner 绕过同一物理设备的唯一控制权。
10. Lease 健康不等于 Task 成功；Task 成功仍由 Verify/Commit 决定。

## 3. Lease 生命周期

```text
AVAILABLE
   │ acquire(task_id, device_id)
   ▼
HELD
   ├── Pause/Takeover ──→ SUSPENDED
   ├── device lost ─────→ INVALID
   ├── Task terminal ───→ RELEASED
   └── controlled stop ─→ RELEASED

SUSPENDED
   ├── Resume + fresh observe + revalidate ─→ HELD
   └── Cancel/timeout/device lost ──────────→ RELEASED/INVALID
```

状态名称用于设计说明，不要求把 Lease 建成新的公共产品状态机。具体私有实现可以沿用现有 lease primitive，只要满足不变量。

## 4. Action 下发栅栏

每个会改变设备状态的 Action 必须按以下顺序通过最后栅栏：

```text
1. Kernel serial queue owns turn
2. Task is RUNNING
3. device_id matches Task
4. Lease is valid for Task/device
5. Action schema is valid
6. based_on_observation_id is current
7. no newer Pause/Cancel/Takeover/input revision
8. persist ActionProposed / physical intent boundary
9. executor dispatches at most one atomic action
10. persist real transport result
11. acquire fresh after-Observation
12. Verify and only then Commit
```

第 8 步后的实现必须保留现有工程已经验证过的 no-replay 原则：如果进程在物理下发结果不明确时崩溃，恢复只能 inspect/observe，不能再次发送原 Action。

## 5. 用户控制语义

### Pause

- Gateway 接受后进入 Kernel 串行命令队列；
- 已经跨过物理下发 seam 的单个动作无法撤回；
- Runtime 结算真实 transport/after-observation/verification 后停止开始新 Action；
- Task 进入 `PAUSED`；Lease 可保持为受控 suspended 或释放，具体是私有实现，但不得授权动作。

### Resume

- 仅从 `PAUSED` 或允许恢复的等待状态接受；
- 重新确认设备和 Lease；
- 获取全新 Observation；
- 必要时重新 PLAN；
- 旧 Action Proposal 一律失效。

### Cancel

- Task 进入终态 `CANCELLED`；
- 不再发出动作；
- Lease 释放；
- 事件与证据不被清除。

### Takeover

- Task 进入 `PAUSED`；
- 自动执行权暂停/释放；
- 用户可以物理操作设备；
- 恢复时必须重新观察并根据真实页面重新规划。

## 6. Legacy 到 New Kernel 的控制权切换

控制权切换必须是单独施工单中的原子运维/代码成果，不得因新包存在就自动发生。

### 状态 1：LEGACY_ACTIVE

- New Kernel 设计或代码可以存在，但不申请目标设备 Lease；
- 当前旧 Runtime 按现状运行；
- 不宣称新 Kernel 已拥有设备。

### 状态 2：DRAINING

- 拒绝创建新的 legacy device work；
- 等待已下发动作完成结算；
- 未下发工作停止在安全 checkpoint；
- 确认 Chat、Learning、Old MobileTask、direct executor route 和外部 Soul owner 没有相同 device_id 的活动物理操作；
- New Kernel 仍不发动作。

### 状态 3：KERNEL_ACTIVE

- New Runtime Kernel 是唯一 Lease 申请方；
- Legacy device writes 固定拒绝或通过 Gateway 翻译为 New Runtime 的高层意图；
- Legacy Adapter 无 Device Port；
- direct executor HTTP route 不再向受 Kernel 管理的 device_id 下发动作；
- 任何外部 owner 若可能控制同一设备，必须纳入独占协调或保持禁用。

不允许：

```text
Old MobileTask ───────────────┐
Chat device loop ─────────────┤
Game Learning ────────────────┼── ADB / same Android
direct executor API ──────────┤
external owner dispatch ──────┤
New Runtime Kernel ───────────┘
```

## 7. 当前可复用资产与限制

可复用：

- `ai_game_console/device_lease.py` 的进程内排他思想；
- `adb_executor.py` 的结构化动作、参数数组、超时和设备核对；
- 现有 MobileTask 的 revision fence、physical intent 和 no-replay 经验；
- shutdown 前拒绝新写入并结算已下发动作的原则。

不能原样假定：

- 当前 lease 已覆盖所有进程和 `F:\dating-copilot` 外部 owner；
- 当前 direct executor route 已经过 Runtime Kernel；
- 当前 Legacy Chat/Learning/MobileTask 已被禁用；
- 当前设备 ready 就等于 Kernel ownership 已切换。

## 8. 多 Client 语义

Web、Hermes、微信或 Soul 可以同时向同一 Active Task 发送消息，但它们都只是 Gateway Client。消息按 Gateway 接收顺序进入 Kernel；任何 Client 都没有单独的设备 worker 或 Lease。

```text
Clients N
   ↓
one Gateway association
   ↓
one Active Task
   ↓
one serial Kernel loop
   ↓
one Device Lease
```

## 9. 验证要求（后续实现阶段）

后续 Device Ownership 施工单至少要证明：

- 两个 Task 不能同时 acquire 同一 device；
- legacy active 时 Kernel 不下发；
- Kernel active 时 legacy/device direct write 被拒绝；
- Pause/Takeover 后无新 Action；
- Resume 先 Observe；
- crash/open-intent 不重放；
- shutdown 结算已下发动作但不开始下一动作；
- 外部 owner 与本地 Kernel 对同一设备不会并行控制。

这些是未来验证要求，不是本 Phase 1 已通过的 Runtime 证据。

## 10. 状态

`DESIGN FROZEN — DEVICE OWNERSHIP CUTOVER NOT EXECUTED`
