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

## 5. 保留给后续施工单

本阶段明确没有实现：

- DeviceExecutionLease、legacy writer drain、外部 owner 互斥或 physical dispatch linearization；
- Android ActionExecutorPort / ADB input；
- Planner / Operator / Language / Router；
- Checkpoint event replay service、loop/STUCK threshold 或自动 recovery ladder；
- Gateway 消息、pause/resume/cancel/takeover、API/SSE、前端；
- 真实 Android smoke 与 MVP battery task。

在 Device Ownership 切换和 Gateway 控制契约完成前，不得把这个 isolated spine 接入真实 Android action path。
