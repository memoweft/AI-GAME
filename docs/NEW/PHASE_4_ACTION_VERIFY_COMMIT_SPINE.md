# Phase 4 — Action / Verify / Commit Spine

> 状态：AUTOMATED ISOLATED SPINE COMPLETE — NO LIVE DEVICE ACTIVATION
>
> 日期：2026-08-11
>
> Store schema：revision 3
>
> 依据：`PHASE_1_DATA_MODEL_DESIGN.md` §7–§12、§18；`07_OBSERVATION_ACTION_VERIFY.md`；`08_RECOVERY_CHECKPOINT_LONG_TASKS.md`

## 1. 目标与边界

本阶段把新 Runtime 已有的 Task / Stage / Event / Observation 持久化脊柱扩展为：

```text
Observation before
  → Action proposal (durable intent)
  → reported ActionExecution transport fact
  → fresh Observation after
  → Verification (SUCCESS | FAIL | UNCERTAIN)
  → SUCCESS-only Fact / Stage / Checkpoint commit
```

实现仍只存在于隔离的 `runtime_kernel/` 与 `runtime_adapters/sqlite/`：

- 没有新增或注册 FastAPI Route；
- 没有前端、Gateway、SSE、Soul 或模型接线；
- 没有 Action executor、ADB input、Lease acquire 或真实设备调用；
- `record_action_execution()` 只持久化测试或未来 physical seam 已报告的 transport 结果，不会发送设备命令；
- 没有改变旧 MobileTask 的任何执行路径、数据库或 DeviceExecutionLease。

因此该阶段证明的是**事实模型、事务和恢复边界**，不是 Android 动作能力或 Device Ownership cutover。

## 2. 领域事实

新增纯 Python domain 包：

```text
runtime_kernel/action/      Action, ActionExecution, transport error
runtime_kernel/verify/      Verification, verdict, method
runtime_kernel/fact/        Fact, scope, provenance status
runtime_kernel/checkpoint/  compact Checkpoint / CheckpointDraft
```

### 2.1 Action 与 transport

- `ActionProposed` 在任何未来 physical dispatch 前已持久化；Action 必须引用当前 Task 的最新 Observation、ACTIVE Stage 和 RUNNING Task。
- `prepare_action_execution()` 是未来 physical seam 的严格决策栅栏：过期 Observation、非活跃 Stage、非 RUNNING Task 或 unresolved checkpoint 中的同一 Action 都被拒绝。
- `ActionExecution.accepted=true` 仅表示 transport accepted；它不会完成 Stage，不会添加 Fact，也不会推进 Task。
- rejected transport 记录为 Action `FAILED` 和可恢复的 Task failure state；它同样不产生已完成进度。

### 2.2 Verify / Commit

- Verification 强制引用 Action 的 before Observation 与一个不同、且仍是 Task 当前 Observation 的 after Observation。
- `FAIL` 与 `UNCERTAIN` 各自持久化判定、更新 failure summary，但不添加 VERIFIED Fact、不完成 Stage；`UNCERTAIN` 不是 Task 顶层终态。
- 只有 `SUCCESS` 进入 `commit_successful_verification()`；同一 SQLite `BEGIN IMMEDIATE` 事务原子写入：
  - Verification；
  - Action 状态；
  - 0..n 个 VERIFIED Fact；
  - 可选的 Stage completion / Task progress；
  - `ActionVerified`、`FactAdded`、`StageCompleted` 事件；
  - 可选 Checkpoint 与最后的 `CheckpointCreated` 事件。
- Fact 永远追加，保留 `supersedes_fact_id`；Store 拒绝跨 Task 的 supersede。

### 2.3 Checkpoint 与 no-replay

Checkpoint 保存紧凑的 goal、Task/Stage 状态、已完成 Stage 摘要、VERIFIED Facts、当前设备摘要、失败摘要、through sequence 与 resume context，不保存模型思考或无限聊天历史。

若 checkpoint 包含 `unresolved_action_ref`：

```text
required_fresh_observation = true
→ 原 Action 不可 prepare / replay
→ 后续必须 Observe 并形成新的 Action decision
```

这使“物理结果未知”保守地下沉为 Action 恢复策略，而不会把新 Task 模型退化为旧系统的顶层 `uncertain` 终态。

## 3. SQLite schema revision 3

`runtime_adapters/sqlite/store.py` 将 revision 2 升为 3，新增：

```text
runtime_actions
runtime_action_executions
runtime_verifications
runtime_facts
runtime_checkpoints
```

关键持久化防线：

- ActionExecution 与 Verification 都对 `action_id` 设唯一约束；
- Action / Verification / Observation / Stage / Fact / Checkpoint 的 Task ownership 在 Store transaction 内复核；
- `runtime_events(task_id, sequence)` 继续在 `BEGIN IMMEDIATE` 内单调递增；
- `Checkpoint.through_sequence` 等于同事务 `CheckpointCreated` 的 sequence；
- 任一 Fact、Stage、Event 或 Checkpoint 写入失败都会回滚同次 SUCCESS commit；
- revision 1 数据库依次迁移至 2 与 3，既有 Task / Stage / Event / Observation 事实不丢失。

## 4. 自动化证据

新增 `test_runtime_kernel_action_verify_commit_spine.py`，覆盖：

1. SUCCESS 的 Verification + VERIFIED Fact + Stage complete + Checkpoint 的原子提交与重开恢复；
2. FAIL / UNCERTAIN 绝不提交 Fact 或 Stage 进度；
3. 新 Observation 使旧 Action decision 无法 dispatch；
4. unresolved intent checkpoint 在进程重开后阻止同一 Action replay；
5. Fact insert 注入失败时，SUCCESS Verification / Action / Stage / Event 全部回滚；
6. rejected transport 不会完成 Stage。

既有 Phase 2/3 tests 同步更新为 schema revision 3 预期，并继续检查 Kernel 无 SQLite / FastAPI / subprocess 等基础设施依赖。

定向结果：

```text
31 passed, 1 existing TestClient deprecation warning
```

完整后端回归结果：

```text
466 passed, 1 existing TestClient deprecation warning
```

## 5. 恢复路径验证（2026-08-17）

Phase 4 恢复承诺已通过 4 个进程重启场景测试验证（`test_runtime_kernel_recovery_paths.py`）：

### 5.1 No-replay 语义

**防护机制**：
- `prepare_action_execution()` 强制 Action 状态必须为 `PROPOSED`
- EXECUTED/FAILED/UNCERTAIN 状态无法重新 dispatch
- `unresolved_action_ref` 提供人工恢复的语义标记

**验证场景**：
```python
# 场景 1: Action 执行后、验证前崩溃
propose_action()  # PROPOSED
prepare_action_execution()
record_action_execution()  # EXECUTED
# 💥 进程崩溃
create_checkpoint(unresolved_action_ref=action_id)
# 重启后：状态检查拒绝重放（不是 PROPOSED）
```

**结论**：状态检查是第一道防线，`unresolved_action_ref` 是防御深度的语义标记。

### 5.2 FAIL vs UNCERTAIN 恢复语义

**FAIL（验证失败，结果明确）**：
- Action 状态 → `FAILED`
- 不写入 Facts，不推进 Stage
- **不自动创建 Checkpoint**
- 允许立即 propose 和 prepare 新 Action（resolved 状态）
- Task 进入 `failure_state`（可恢复）

**UNCERTAIN（物理结果不确定）**：
- Action 状态 → `UNCERTAIN`
- 不写入 Facts，不推进 Stage
- **不自动创建 Checkpoint**（需要手动调用 `create_checkpoint()`）
- Task 进入 `failure_state`（可恢复）
- 如果手动创建 Checkpoint 带 `unresolved_action_ref`，则阻止重放

**设计空白**：
- UNCERTAIN 场景是否应该自动创建 Checkpoint？
- 当前需要外层决策逻辑显式调用 `create_checkpoint(unresolved_action_ref=...)`
- 建议：在 Phase 5 前明确 UNCERTAIN 的自动化策略

### 5.3 人工恢复决策流程

当 `unresolved_action_ref` 存在时：

```text
1. 加载 Checkpoint
   ├─ unresolved_action_ref: action-123
   ├─ required_fresh_observation: true
   └─ resume_reason: "uncertain physical outcome"

2. 人工检查物理设备状态
   ├─ 查看最后的 Observation after
   ├─ 查看 Action 意图和预期结果
   └─ 检查当前设备屏幕

3. 决策路径
   ├─ A. 补偿：手动标记 Action 为 VERIFIED + 添加 Facts
   ├─ B. 重试：capture_observation() → propose_action()（新 Action）
   ├─ C. 跳过：标记 Action 为 FAILED → 继续新决策
   └─ D. 终止：close Task
```

**当前状态**：
- ✅ 数据模型支持：Checkpoint 记录 `unresolved_action_ref`
- ✅ 恢复检测：`prepare_action_execution()` 拒绝重放
- ⏸️ UI/API：无人工恢复界面
- ⏸️ 策略：无自动补偿/重试/跳过逻辑

### 5.4 跨进程恢复正确性

**验证结果**：
- SQLite 持久化完整：Action/Verification/Fact/Checkpoint 全部正确恢复
- Checkpoint 状态重建：`unresolved_action_ref`、`required_fresh_observation` 正确
- 事件序列连续：`through_sequence` 正确，新事件单调递增
- ID 生成安全：UUID 避免跨进程冲突

**测试覆盖**：
- 4 个进程重启场景（`test_runtime_kernel_recovery_paths.py`）
- 35 个 runtime_kernel 测试通过
- 完整回归：470 个后端测试通过

### 5.5 剩余恢复风险

**已解决**：
- ✅ No-replay 语义：状态检查 + checkpoint 标记
- ✅ 原子提交回滚：Fact 插入失败时完整回滚
- ✅ 跨进程状态恢复：数据库持久化正确

**待实现**：
- ⏸️ UNCERTAIN 自动 Checkpoint 策略
- ⏸️ 人工恢复 UI/API（补偿/重试/跳过）
- ⏸️ DeviceExecutionLease 的恢复语义（Phase 5）
- ⏸️ 自动补偿策略（基于 Action 类型的幂等性）

**文档**：
- `PHASE_4_RECOVERY_VERIFICATION.md`：完整恢复场景分析

## 6. 保留给后续施工单

本阶段明确没有实现：

- DeviceExecutionLease、legacy writer drain、外部 owner 互斥或 physical dispatch linearization；
- Android ActionExecutorPort / ADB input；
- Planner / Operator / Language / Router；
- Checkpoint event replay service、loop/STUCK threshold 或自动 recovery ladder；
- 人工恢复决策 UI/API（补偿/重试/跳过/终止）；
- Gateway 消息、pause/resume/cancel/takeover、API/SSE、前端；
- 真实 Android smoke 与 MVP battery task。

在 Device Ownership 切换和 Gateway 控制契约完成前，不得把这个 isolated spine 接入真实 Android action path。
