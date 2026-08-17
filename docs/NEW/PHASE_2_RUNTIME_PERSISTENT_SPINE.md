# Phase 2 — Runtime Kernel Persistent Spine

执行日期：2026-08-10

状态：`PHASE 2 DONE — STOP`

## RESULT

Phase 2 已完成一个尚未接线、可独立测试和持久化恢复的新 Runtime 事实脊柱：

```text
Task + Stage + RuntimeEvent
          ↓
RuntimeStorePort
          ↑
SQLiteRuntimeStore
          ↑
minimal RuntimeKernel service
```

新 Kernel 可以在不依赖 Legacy MobileTaskRuntime、FastAPI、ADB、GUI-Owl 或其他模型能力的情况下完成：

```text
Create Task
→ Load Task
→ Create Stage
→ Start Stage
→ Complete Stage with evidence
→ Read task-local ordered events
→ Close all operation-scoped connections
→ Reopen the SQLite database
→ Recover the same committed facts
```

本阶段没有注册 API、没有接入 Console startup、没有创建正式 `runtime.db`、没有操作 Android，也没有开始 Phase 3。

## CHANGES

新增正式源码包：

- `apps/console/backend/ai_game_console/runtime_kernel/__init__.py`
- `apps/console/backend/ai_game_console/runtime_kernel/kernel.py`
- `apps/console/backend/ai_game_console/runtime_kernel/ports.py`
- `apps/console/backend/ai_game_console/runtime_kernel/task/__init__.py`
- `apps/console/backend/ai_game_console/runtime_kernel/task/domain.py`
- `apps/console/backend/ai_game_console/runtime_kernel/stage/__init__.py`
- `apps/console/backend/ai_game_console/runtime_kernel/stage/domain.py`
- `apps/console/backend/ai_game_console/runtime_kernel/event/__init__.py`
- `apps/console/backend/ai_game_console/runtime_kernel/event/domain.py`

新增基础设施 Adapter：

- `apps/console/backend/ai_game_console/runtime_adapters/__init__.py`
- `apps/console/backend/ai_game_console/runtime_adapters/sqlite/__init__.py`
- `apps/console/backend/ai_game_console/runtime_adapters/sqlite/store.py`

新增自动测试：

- `apps/console/tests/backend/test_runtime_kernel_persistent_spine.py`

新增本报告：

- `docs/NEW/PHASE_2_RUNTIME_PERSISTENT_SPINE.md`

没有修改 Legacy Domain、既有 API、Console startup、前端、ApplicationRuntime、DeviceExecutionLease、Game Learning、Soul owner 或 `F:\dating-copilot`。

项目及已检查父目录不包含可用的 Git repository，因此不能提供 branch、HEAD 或 Git diff；本阶段没有执行 reset、clean、commit 或 push。

## DOMAIN

### Task

实现字段与 Phase 1 冻结模型一致：

- `id`
- `schema_version`
- `goal`
- `status`
- `source.client_id`
- `source.conversation_id`
- `source.initial_message_id`
- `device_id`
- `current_stage_id`
- `last_observation_id`
- `last_meaningful_progress_at`
- `failure_state`
- `latest_checkpoint_id`
- `created_at`
- `updated_at`
- `terminal_at`

状态使用：

```text
CREATED / PLANNING / RUNNING / WAITING / PAUSED / STUCK /
COMPLETED / FAILED / CANCELLED
```

领域对象是 frozen dataclass。构造时验证必填字段、UTC timestamp、schema version、终态时间等结构不变量；`transition_to` 只接受冻结状态图允许的转换。Phase 2 的最小 Stage 路径为：

```text
Task CREATED
→ start Stage
→ PLANNING
→ RUNNING + current_stage_id
→ complete Stage
→ PLANNING + current_stage_id=null
```

其中 CREATED 到 RUNNING 的 `PLANNING` 是同一次 Stage 启动领域转换中的中间合法状态，不单独落盘为不完整事实。

### Stage

实现字段与 Phase 1 冻结模型一致：

- `id`
- `task_id`
- `ordinal`
- `objective`
- `completion_criteria`
- `status`
- `planner_call_id`
- `progress_summary`
- `started_at`
- `completed_at`
- `evidence_refs`

状态使用：

```text
PENDING / ACTIVE / COMPLETED / FAILED / SUPERSEDED
```

本阶段 Public Service 实现 `PENDING → ACTIVE → COMPLETED`。完成 Stage 必须显式提供至少一个非空 evidence reference；因为 Verification 尚在 OUT OF SCOPE，测试使用明确的可审计测试 evidence reference，不伪造 Verification 模型。

### RuntimeEvent

实现字段与 Phase 1 冻结模型一致：

- `id`
- `task_id`
- `sequence`
- `type`
- `actor`
- `payload`
- `causation_id`
- `correlation_id`
- `created_at`
- `schema_version`

当前最小 Service 写入：

- `TaskCreated`
- `StageCreated`
- `StageStarted`
- `StageCompleted`

`TaskCreated` 包含 goal、source、device_id；`StageStarted` 包含 stage id、ordinal、objective、completion criteria；`StageCompleted` 包含 evidence references。`StageCreated` 用于保证显式创建并持久化 PENDING Stage 时也有对应的事实事件。

## STORE

### 依赖方向

依赖方向保持为：

```text
Runtime Kernel → RuntimeStorePort ← SQLiteRuntimeStore
```

Kernel 不 import `sqlite3`、SQL、SQLite connection/Row 或数据库路径。具体数据库能力全部位于 `runtime_adapters/sqlite`。

### 最小 Schema

测试数据库只创建：

- `runtime_schema`：本地 schema revision 元数据；
- `runtime_tasks`：Task 当前事实；
- `runtime_stages`：Stage 当前事实；
- `runtime_events`：不可混用全局 ID 推导的 Task-local 有序事件。

没有提前创建 Observation、Action、Verification、Fact、Checkpoint、RoleCall、ClientMessage 或 Idempotency 表。

关键约束：

- `runtime_stages(task_id, ordinal)` 唯一；
- partial unique index `runtime_stages(task_id) WHERE status='ACTIVE'`；
- `runtime_events(task_id, sequence)` 唯一；
- Task、Stage 和 Event 通过 foreign key 隔离所属 Task；
- frozen enum 使用 SQLite `CHECK` 限制非法持久状态。

### Transaction 与 sequence

所有写操作使用独立连接与：

```text
BEGIN IMMEDIATE
→ write state mutation
→ calculate MAX(sequence)+1 for this task only
→ insert corresponding event
→ COMMIT
```

任一步抛错则：

```text
ROLLBACK state + event
```

Task-local sequence 在持有 SQLite write lock 的同一事务中计算并写入；同时有 `(task_id, sequence)` 唯一约束作为持久化防线。它不依赖全局 rowid，数据库重开后会从已提交的 Task 最大 sequence 继续。

所有连接均为 operation-scoped，并在读写操作结束时显式关闭。`RuntimeKernel.close()` 仍可表达生命周期边界；Adapter 不保留隐藏的常驻 connection。

## INVARIANTS

| 不变量 | 保证方式 | 自动测试证据 |
|---|---|---|
| Task persistence | 全字段编码/解码，关闭后由新 Store 实例加载 | `test_task_persists_and_recovers_after_store_reopen` |
| Stage persistence | Task 与 Stage 分表持久化，Task 保存 active reference | `test_stage_and_current_stage_persist_after_reopen` |
| Single Active Stage | Task Domain 拒绝第二个 active reference；SQLite partial unique index 再拒绝 | `test_single_active_stage_is_rejected_by_domain_and_database` |
| Task-local sequence | `BEGIN IMMEDIATE` 内按 task 查询最大 sequence；复合唯一约束 | `test_event_sequence_is_task_local_and_continues_after_reopen` |
| Task isolation | 所有 Stage/Event 查询带 `task_id`，foreign key 和复合约束隔离 | `test_tasks_keep_stages_events_and_sequences_isolated` |
| Task create atomicity | Task insert 与 TaskCreated 共用事务，注入 event failure 后全回滚 | `test_task_creation_rolls_back_when_event_insert_fails` |
| Stage mutation atomicity | Task update、Stage update、Event insert 共用事务，注入失败后全回滚 | `test_stage_mutation_rolls_back_when_event_insert_fails` |
| Invalid transition | frozen Task/Stage 状态图在领域层拒绝 | `test_invalid_task_and_stage_transitions_are_rejected` |
| Restart recovery | Process A 与 Process B 使用不同 Kernel/Store 实例复核全量事实相等 | `test_restart_recovery_preserves_all_committed_facts` |
| Minimal schema | 直接检查临时 SQLite 的 table 集合 | `test_schema_contains_only_phase_2_fact_spine_tables` |
| Legacy isolation | AST import 检查 + 禁止领域概念扫描 | `test_runtime_kernel_has_no_legacy_or_infrastructure_imports` |

## VERIFICATION

### 新 Runtime 定向测试

命令：

```powershell
cd F:\AI-GAME\apps\console\backend
& '..\..\..\runtime\envs\console\Scripts\python.exe' `
  -m pytest '..\tests\backend\test_runtime_kernel_persistent_spine.py' -q
```

最终结果：

```text
11 passed, 1 warning in 1.04s
```

### 完整 backend regression

命令：

```powershell
cd F:\AI-GAME\apps\console\backend
& '..\..\..\runtime\envs\console\Scripts\python.exe' `
  -m pytest '..\tests\backend' -q
```

最终结果：

```text
438 passed, 1 warning in 64.71s
```

Phase 0 原有 427 项全部未回归，新增 11 项全部通过。唯一 warning 与 Phase 0 相同，来自 FastAPI TestClient 对 Starlette/httpx 组合的弃用提示，不是本阶段产生的失败。

前端没有修改，因此按施工单不重复运行前端测试。

## RUNTIME

完成代码和测试后的只读核验：

- `F:\AI-GAME\runtime\console\runtime.db`：不存在；
- Console：仍由原 PID `35212` 监听 `127.0.0.1:4310`；
- Console `/api/v1/health`：`status=ok`、`service=ai-game-console`、`version=0.1.0`、`database=ready`；
- GUI-Owl：仍由原 PID `35108` 监听 `127.0.0.1:4243`；
- 在线 OpenAPI 仍包含既有 `/api/v1/tasks`、`/api/v1/tasks/{task_id}`、`/inputs`、`/stop`；没有注册本阶段 Kernel/Gateway API；
- 没有停止或重启 Console；
- 没有停止或重启 GUI-Owl；
- 没有调用 ADB、没有读取或改变 Android UI、没有创建设备动作；
- 没有创建 Legacy MobileTask。

`database=ready` 指在线旧 Console 原有数据库健康状态，不代表新 `runtime.db` 已激活；新正式数据库仍不存在。

## BOUNDARY

本阶段明确没有实现或接线：

- Observation / Screenshot / UI Tree / Device State；
- Action / ActionExecution；
- Verification / Verify-Commit loop；
- Planner / Operator / Language；
- Model Router / GUI-Owl / 云模型；
- Context Builder / Recovery / Checkpoint / Facts；
- Gateway、messages、controls、Runtime events API 或 SSE；
- Client routing / Idempotency；
- DeviceExecutionLease cutover / ADB；
- Legacy Archive API / Legacy Adapter；
- Soul Migration / Hermes / 微信 / Chat Workbench；
- Console startup activation 与正式 `runtime.db`。

## DEVIATIONS

没有改变 Phase 1 冻结字段、状态或事务核心语义。

局部实现选择：

1. 新增 `StageCreated` 事实事件。施工单要求每次状态/事实写入有对应事件，但 Phase 1 核心事件表只列出运行时的 `StageStarted`。为了避免 PENDING Stage 已落库而无事件，本阶段为显式 Stage 创建增加 `StageCreated`；未新增角色或未来能力。
2. 使用一个最小 `runtime_schema` 元数据表记录 schema revision。它不承载未来业务模型，也不预建未来表。
3. `Store.close()` 是显式生命周期方法，但 SQLite adapter 使用 operation-scoped connection，因此 close 不需要关闭常驻资源。测试中的“关闭/重开”通过销毁 Process A 的 Kernel/Store 使用并创建全新的 Process B 实例完成。

## OPEN FINDINGS

以下问题留给后续施工单，不在 Phase 2 越权处理：

1. `create_stage` 的 ordinal 目前由最小 Kernel 读取现有 Stage 后计算，数据库唯一约束会安全拒绝并发冲突，但未来 Planner/Runtime loop 若允许并发规划，需要冻结“单写者串行化”或 Store 内 ordinal 分配/重试策略。
2. `current_stage_id` 与 ACTIVE Stage 的共同更新已经由 Kernel transaction 保证，SQLite partial unique index保证最多一个 ACTIVE；是否追加数据库级双向一致性 trigger，应在未来 migration 设计中决定，不能在 Phase 2 擅自扩大 schema 机制。
3. schema revision 目前只有 revision 1；正式 activation、迁移 runner、备份和故障恢复策略尚未施工。
4. Stage FAILED/SUPERSEDED、Task WAITING/PAUSED/STUCK/终态转换的领域状态图已冻结并可验证，但最小 Public Service 没有提前暴露这些尚无 Phase 2 行为来源的命令。
5. RuntimeEvent 目前是事实存储；事件投影、Gateway streaming、SSE reconnect 和 checkpoint compaction 均留待后续阶段。

## STOP

```text
PHASE 2 DONE — STOP
```

没有继续 Phase 3、Observation、ADB、Operator、Verify 或 Gateway。
