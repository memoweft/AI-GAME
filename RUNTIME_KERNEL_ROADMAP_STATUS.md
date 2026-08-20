# Runtime Kernel 实施路线图与当前状态

**更新日期**: 2026-08-20  
**项目**: AI-GAME Console - 隔离 Runtime Kernel  
**仓库**: https://github.com/memoweft/AI-GAME

---

## 🎯 总体目标

构建一个隔离的、可持久化的 Runtime Kernel，实现：
- Goal → Stage → Action → Verify → Commit 完整执行脊柱
- 原子性恢复语义（无重放风险）
- 设备独占控制和 Lease 管理
- 与 Legacy API 零依赖的独立边界

---

## 📊 阶段总览

| Phase | 名称 | 状态 | 测试 | 说明 |
|-------|------|------|------|------|
| Phase 0 | 当前基线 | ✅ 完成 | - | 现有代码分析 |
| Phase 1 | 边界设计 | ✅ 冻结 | - | 数据模型、端口、不变量 |
| Phase 2 | 持久化脊柱 | ✅ 完成 | 26 passed | Task/Stage/Observation 基础 |
| Phase 3 | Observation 脊柱 | ✅ 完成 | 17 passed | 捕获、通道、老化逻辑 |
| Phase 4 | Action→Verify→Commit | ✅ 完成 | 18 passed | 原子提交、恢复策略 |
| **Phase 5** | **设备所有权** | **✅ 完成** | **79 passed** | **Lease、执行器、排空、Deadline、管理 API、E2E 集成** |
| **Phase 6** | **Gateway 契约** | **✅ 完成** | **159 passed (Week 1-4 + 集成)** | **Week 1 控制面 + Week 2 应用服务 + Week 3 HTTP API + Week 4 SSE 流 + 客户端投影 + 控制 UI + 集成（§17 10/10、场景 10 待设备）；验收见 `PHASE_6_INTEGRATION_ACCEPTANCE.md`** |
| **Phase 7** | **Legacy 迁移** | **✅ 完成** | **708 passed (Week 1-3 + 集成)** | **三阶段运行时模式切换 + 切流前快照 + 排空门禁 + 监控/回滚；验收 7/7，见 `PHASE_7_INTEGRATION_ACCEPTANCE.md`** |

**当前测试总数**: 708 passed（全量回归，2026-08-20；Phase 7 集成完成后，~176s）+ 前端 52 passed

---

## ✅ 已完成内容

### Phase 2: 持久化脊柱 (26 tests)

**核心领域模型**:
- `Task`: Goal → Stage 状态机，PLANNED → RUNNING → FAILED/SUCCEEDED
- `Stage`: 阶段目标和完成标准，PLANNED → ACTIVE → SUCCEEDED/ABANDONED
- `Checkpoint`: 可恢复的任务快照，支持 Reopen

**持久化**:
- SQLite schema revision 1-2
- `SQLiteRuntimeStore` 实现（原子事务、乐观并发控制）
- Event 日志（Task/Stage 生命周期事件）

**关键测试**:
- 并发竞争条件（乐观锁）
- Stage 序列化一致性
- Checkpoint Reopen 语义

---

### Phase 3: Observation 脊柱 (17 tests)

**Observation 生命周期**:
- `capture_observation()`: 捕获设备快照（screenshot + UI tree）
- 通道可用性检测（ScreenshotChannel/UiTreeChannel）
- 老化逻辑：stale_if_action_was_proposed, stale_if_new_observation_captured

**Artifact 存储**:
- `ArtifactStorePort`: 分离内容寻址存储（screenshot PNG、UI JSON）
- 假实现 `FakeArtifactStore`（内存字典）

**关键不变量**:
- Observation 必须基于 ACTIVE Stage
- Action 只能基于最新的 Observation
- 新 Observation 使旧的失效

---

### Phase 4: Action→Verify→Commit 脊柱 (18 tests)

**Action 生命周期**:
- PROPOSED → EXECUTED → VERIFIED/FAILED/UNCERTAIN
- `propose_action()`: 基于 Observation 提案
- `execute_action()`: 真实 ADB 执行（集成 `adb_executor.py`）
- `record_execution()`: 记录执行结果

**Verification 和 Commit**:
- `verify_action()`: 人工或规则验证
- `commit_verification()`: 原子提交 Verification + Facts + Stage 进度 + Checkpoint
- SUCCESS → 提交 Facts，推进 Stage
- FAIL/UNCERTAIN → 不提交，创建 Checkpoint

**恢复语义**:
- EXECUTED 未 Verify: 阻止重放（`unresolved_action_ref`）
- UNCERTAIN: 需要人工介入
- FAIL: 已解决，不阻止
- Checkpoint 去重和快照隔离

**关键文档**:
- `PHASE_4_RECOVERY_VERIFICATION.md`: 恢复场景分析
- `PHASE_4_RECOVERY_STRATEGY.md`: UNCERTAIN 自动 Checkpoint 设计

---

### Phase 5 Week 1-3: DeviceExecutionLease (15 tests)

**Lease 数据模型**:
- `DeviceExecutionLease`: 设备独占权证明（TTL 60s）
- SQLite schema revision 4: `runtime_device_leases` 表
- UNIQUE(device_id) 约束确保设备互斥

**Store Port 扩展**:
- `acquire_lease()`: 获取独占权（冲突抛出 `LeaseConflict`）
- `renew_lease()`: 续期（更新 expires_at）
- `release_lease()`: 释放
- `list_expired_leases()`: 查询过期 Lease
- `update_lease_action()`: 关联当前 Action

**RuntimeKernel 集成**:
- `execute_action()` 在执行前获取 Lease
- 执行后自动释放 Lease
- 失败时也释放 Lease

**RuntimeMode**:
- `legacy` / `draining` / `kernel_active` 三阶段模式
- `RuntimeModeGuard`: 运行时检查和拒绝
- 环境变量配置 `RUNTIME_MODE`

**关键测试**:
- Lease 独占性（并发 acquire 冲突）
- 续期逻辑
- 过期 Lease 查询
- Action 关联追踪
- RuntimeMode 切换和守卫

---

### Phase 5 Week 4: DeviceLeaseManager ✅ (5 tests)

**后台清理线程**:
- `DeviceLeaseManager`: 定期扫描过期 Lease
- daemon 线程，interval 可配置（默认 30s）
- 分段 sleep 支持快速停止

**进程存活检测**:
- 跨平台实现：
  - Windows: `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`
  - Unix: `os.kill(pid, 0)` with PermissionError 处理
- `_is_process_alive(process_id: str) -> bool`

**孤立 Lease 恢复**:
- 检测进程已死的 Lease
- 为 PROPOSED/EXECUTED Action 创建 Checkpoint
- 使用 `unresolved_action_ref` 阻止重放
- 在 snapshot 记录恢复上下文

**异常隔离**:
- 顶层 try-except 保护后台线程
- 每个 Lease 操作独立捕获异常
- 清理失败不影响其他 Lease

**RuntimeKernel 集成**:
- 新增 `lease_manager` 可选参数
- 新增 `shutdown()` 方法
- 后台线程启动由调用方显式控制

**关键测试**:
- 清理过期 Lease
- 为孤立 Action 创建 Checkpoint
- 后台线程生命周期
- 异常处理容错
- 进程存活检测

**文档**:
- `PHASE_5_WEEK_4_DEVICE_LEASE_MANAGER.md`

---

### Phase 5 Week 7: 端到端集成测试 ✅ (Day 1-5 全部完成, 20 个测试, 2026-08-19 收官)

> 注：原计划的 Week 5（Deadline 保护）和 Week 6（管理 UI/API）曾延后，
> 项目直接进入 Week 7 端到端集成测试；两周已于 2026-08-19 补做完成（见下节）。

**测试文件**:
- `tests/backend/test_runtime_kernel_e2e_integration.py`（5 tests）
- `tests/backend/test_runtime_kernel_e2e_concurrency.py`（4 tests）
- `tests/backend/test_runtime_kernel_execute_action.py`（8 tests，含 Day 3 增强）
- `tests/backend/test_runtime_kernel_e2e_performance.py`（3 tests，Day 4 基准）

**Day 1 — 进程崩溃恢复与过期清理** (commit b1f1810):
- 孤立 Lease 恢复：死 PID 检测 → Checkpoint(unresolved_action_ref) → 释放
- 过期 Lease 清理：存活 PID + 过期 TTL → 警告日志 → 释放 + Checkpoint
- Checkpoint 去重：重复扫描不创建重复 Checkpoint

**Day 2 — 多 Task 并发与后台线程稳定性** (commit 6a508cd):
- 不同设备上的多 Task 并发执行互不干扰
- 并发 acquire 冲突：仅一个成功，另一个 `LeaseConflict`
- 后台清理线程长期运行稳定性 + 异常容错隔离

**Day 3 — execute_action 完整流程** (2026-08-19):
- `propose → execute → verify → commit` 完整链路集成测试
- SUCCESS 裁决：commit Facts + Stage COMPLETED + Task 回到 PLANNING + Checkpoint(stage_completed)
- FAIL 裁决：Action FAILED、Stage 保持 ACTIVE、失败记录不阻断、可基于新 Observation 恢复
- 类型化执行器全覆盖（tap/swipe/input_text/back/home 5 种分发）
- 栅栏增强：设备 Lease 冲突（`LeaseConflict`）、防重放、防过期决策

**Day 4 — 性能基准** (2026-08-19):
- acquire/renew/release 单操作：中位数 ~7.9ms、p99 ~9ms（200 轮 × 3 续期）
- 50 设备并发获取+释放：总计 690ms
- 后台清理线程空闲 CPU：**0.16%**（达标 <1% 目标，无 busy-loop）

**全量回归**: 510 passed（~118s）

**文档**:
- `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`（10 个场景，9 个已验证 ✅，场景 10 可选）

---

## ✅ Phase 5 补做工作（Week 5-6，2026-08-19 全部完成）

### Week 5: Deadline 保护机制 ✅（补做完成, 2026-08-19, 10 tests）

**目标**: 防止 Lease 无限续期，确保异常情况下任务最终超时

**实现内容**:
1. **Deadline 模型扩展**
   ```python
   @dataclass(frozen=True)
   class DeviceExecutionLease:
       # ... existing fields
       deadline_at: str  # 绝对截止时间（不可续期延长）
   ```

2. **续期限制**
   - `renew_lease()` 检查 `now < deadline_at`
   - 超过 Deadline 抛出 `LeaseExpired`
   - 默认 Deadline = acquired_at + 5 分钟

3. **Deadline 触发清理**
   - `_cleanup_orphaned_leases()` 扫描超 Deadline 的 Lease
   - 即使进程存活，也创建 Checkpoint 并释放
   - 记录 `deadline_exceeded` 恢复原因

4. **测试**
   - Deadline 阻止续期
   - Deadline 触发自动清理
   - Deadline 超时创建 Checkpoint

**文档**:
- `PHASE_5_WEEK_5_DEADLINE_PROTECTION.md`

---

### Week 6: 管理 UI/API ✅（补做完成, 2026-08-19, 9 tests）

**目标**: 提供 Lease 状态查询和手动干预能力

**实现内容**:
1. **只读查询 API**
   ```python
   GET /api/runtime/leases
   GET /api/runtime/leases/{lease_id}
   GET /api/runtime/leases?device_id={device_id}
   GET /api/runtime/leases?task_id={task_id}
   ```

2. **管理操作 API**
   ```python
   POST /api/runtime/leases/{lease_id}/release  # 手动释放
   POST /api/runtime/leases/cleanup              # 手动触发清理
   GET /api/runtime/leases/stats                 # 统计信息
   ```

3. **监控指标**
   - 当前活跃 Lease 数量
   - 过期/孤立 Lease 数量
   - 清理执行次数和错误率
   - Lease 平均持有时间

4. **前端页面**（可选）
   - Lease 列表视图
   - 实时状态刷新
   - 手动释放按钮

**测试**:
- API 端点集成测试
- 权限控制（管理员操作）

**文档**:
- API 规范（OpenAPI）
- 运维手册

---

### Week 7: 端到端集成测试 (3-5 天, ✅ 完成, 2026-08-19 收官)

**目标**: 验证完整的 Lease 生命周期和恢复流程

**测试场景**（详见 `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`）:
1. ✅ **并发冲突** (Day 2)：并发 acquire 仅一个成功，另一个 `LeaseConflict`
2. ✅ **进程崩溃恢复** (Day 1)：孤立 Lease 检测 → Checkpoint(unresolved_action_ref)
3. ✅ **Deadline 超时**：Week 5 补做完成 —— 超 Deadline 强制回收（含存活进程）+ `deadline_exceeded` Checkpoint（见下节 Week 5）
4. 🎯 **真实设备测试**（可选，低优先级）
5. ✅ **性能测试** (Day 4)：Lease 续期开销中位数 ~7.9ms、清理线程空闲 CPU 0.16%

**剩余**: 无（场景 10 真实设备测试为可选，留待 Phase 6 前冒烟）

**文档**:
- ✅ 测试报告 + 故障排查指南 + 代码审查 (Day 5) → `docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md`
- ✅ 性能基准 (Day 4)
- ✅ 故障排查指南 (Day 5, 首版：10 类症状 + SQL 诊断速查)

**Day 5 — 测试报告与收官**:
- 编写 `docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md`（场景覆盖表、20 测试清单、Day 4 基准数据、关键发现、资源泄漏检查、剩余风险）
- 故障排查指南（首版）：LeaseConflict / LeaseNotFound / 孤立未回收 / 过期警告 / Checkpoint 阻塞 / 防重放 / stale 决策 / 清理异常 / SQLite 锁 / CPU 异常
- 代码审查：4 个 Week 7 测试文件导入干净、Fake 一致、生命周期纪律（close/shutdown）、tmp_path 隔离，无需重构
- 全量回归 510 passed 保持不变

---

## 🎯 Phase 5 成功标准

**Phase 5 已于 2026-08-19 全部完成。** 完成的定义：
- ✅ Week 1-4 完成（40 tests passed）
- ✅ Week 7 完成（Day 1-5，20 tests；测试报告 + 故障排查指南 + 代码审查齐备）
- ✅ Week 5（Deadline 保护）补做完成（10 tests，2026-08-19）
- ✅ Week 6（管理 UI/API）补做完成（9 tests，2026-08-19）
- ✅ 所有测试通过（实际：529 passed，2026-08-19）
- ⏳ 真实设备执行 tap/swipe/back/home 成功（可选，低优先级）
- ✅ 两个 Task 无法同时 acquire 同一设备（Day 2 并发冲突测试）
- ✅ Lease 过期后自动清理（Day 1 过期清理测试）
- ✅ 进程崩溃后孤立 Lease 创建恢复 Checkpoint（Day 1 崩溃恢复测试）
- ✅ Deadline 超时触发自动释放（Week 5：超 Deadline 强制回收，持有进程存活与否均回收）
- ✅ 管理 API 可查询和手动干预（Week 6：`/api/v1/runtime/leases` 系列端点）
- ✅ 文档完整：Week 7 测试报告 + 故障排查指南 + Week 5 Deadline 文档 + Week 6 API/运维文档

---

## ✅ Phase 6: Gateway 契约（完成）

**契约**: `docs/NEW/PHASE_1_GATEWAY_CONTRACT_DESIGN.md`（DESIGN FROZEN — API NOT MODIFIED）  
**实施计划**: `PHASE_6_GATEWAY_CONTRACT_PLAN.md`（4 周 + 集成）  
**验收**: `PHASE_6_INTEGRATION_ACCEPTANCE.md`（§17 全清单 10/10，场景 10 待设备）  
**启动说明**: 场景 10 真机冒烟（可选）并入 Phase 6 集成阶段（当前机器无 adb，2026-08-20 决策）

### Phase 6 Week 1: Kernel 控制面 ✅（2026-08-20, 25 tests）

**目标**: 契约 §9 的 pause / resume / cancel / takeover 落到 Kernel，事件先持久化后应答。

**实现内容**:
1. **`runtime_kernel/control/` 领域模块**
   - `ControlCommand`（pause/resume/cancel/takeover）、`ControlError`、`InvalidControlTransition`、`ControlResult(task, event, released_lease_id)`
   - 命令 → 状态映射复用冻结迁移图（不修改 `task/domain.py`）：
     - pause: RUNNING → PAUSED，事件 `TaskPaused`
     - resume: PAUSED → RUNNING，事件 `TaskResumed`（**不重新获取 Lease**，Runtime 必须先 Observe）
     - cancel: 任意非终态 → CANCELLED，事件 `TaskCancelled`（终态 `terminal_at` 置位）
     - takeover: RUNNING → PAUSED，事件 `UserTakeover`（与 pause 仅事件类型与客户端投影不同）
2. **`RuntimeKernel.apply_control(*, task_id, command, reason=None)`**
   - 接受 `ControlCommand` 或原始字符串（未知命令 → `ControlError`）
   - 事件 actor=`gateway`，payload `{"previous_status": ..., "reason": ...}`
   - pause/cancel/takeover 释放 Task 当前持有 Lease（`release_lease` 幂等删除，与 `execute_action` finally 无竞态）
3. **Store 端口**: 新增 `mutate_task(before_task, after_task, event)`（任务级原子变更 + 事件，CAS 模式同 `mutate_task_and_stage`，无 Stage 检查）；SQLite 实现
4. **导出**: `runtime_kernel/__init__.py` 增加 4 个 control 符号

**测试**（`tests/backend/test_runtime_kernel_controls.py`, 25 passed）:
- 每命令 × 合法/非法状态；lease 释放/保留；takeover 与 pause 投影差异
- 事件序列严格递增、actor=gateway、reason 持久化
- 未知 Task → `RecordNotFound`；未知命令 → `ControlError`
- `mutate_task` CAS：stale before_task / 身份变更 / 原子持久化

**全量回归**: **554 passed**（2026-08-20）

---

### Phase 6 Week 2: Gateway 应用服务包 ✅（2026-08-20, 59 tests）

**目标**: 契约 §3-§9 的应用服务层 —— Gateway 只调 Kernel 公共服务，不碰 Store 表、不碰 ADB、不建第二套 Active Task 状态机（§15）。

**新增包**: `backend/ai_game_console/gateway/`

1. **`errors.py` — §14 冻结错误表**
   - `GatewayError(code, message, retryable, details)` + `to_dict()`；10 个子类精确对应契约码（`VALIDATION_ERROR` / `TASK_NOT_FOUND` / `TASK_NOT_ACTIVE` / `DEVICE_NOT_FOUND` / `DEVICE_NOT_AVAILABLE`(retryable) / `CONVERSATION_CONFLICT` / `IDEMPOTENCY_CONFLICT` / `EVENT_CURSOR_INVALID` / `LEGACY_TASK_WRITE_DISABLED` / `INTERNAL_ERROR`）
2. **`store.py` — `GatewayStore`（SQLite，独立 `gateway.db`）**
   - `gateway_schema(revision PK, applied_at)` + `gateway_idempotency(scope, key, payload_hash, response_json, created_at, PK(scope,key))`
   - 初始化三态：新建 / 幂等重入 / 冲突（无 schema 表或更高 revision → `GatewayStoreConflict`）
   - 每操作独立连接 + `BEGIN IMMEDIATE`
3. **`idempotency.py` — §3 幂等语义**
   - `canonical_payload_hash`: sha256 + JSON 规范化（sort_keys、紧凑分隔、ensure_ascii=False）
   - `IdempotencyService.execute_once(scope, key, payload, operation)`：空白 key → `VALIDATION_ERROR`；同 key 同 payload → 逐字重放存储响应（操作恰好执行一次）；同 key 不同 payload → `IDEMPOTENCY_CONFLICT`；操作失败不落库（可安全重试）；`put` 撞 IntegrityError 时重查存储记录裁决
   - 4 个 scope：`task.create` / `task.message` / `task.control` / `conversation.message`
4. **`device_registry.py` — 设备可用性端口**
   - `DeviceSummary(device_id, status)` + `DeviceRegistry` Protocol（`list_devices` / `get_device`）；Week 3 接真实 ADB 实现
5. **`conversations.py` — `ConversationService`（§8）**
   - `active_tasks`: 会话内非终态 Task；`resolve`: 0 个 → None，1 个 → 该 Task，2+ 个 → `CONVERSATION_CONFLICT`（**绝不按最近猜测**）
6. **`snapshot.py` — §6 Gateway Snapshot 投影**
   - `build_task_snapshot(kernel, task)`: `current_stage`（跳过 COMPLETED）、`completed_stages`、`verified_facts`（仅 Kernel 已验证事实）、`last_event_sequence`（事件日志末序）、`constraints=[]`（尚无约束模型）、`last_observation_id` / `updated_at`
7. **`task_gateway.py` — `TaskGateway`（§5/§7/§8/§9 入口）**
   - `create_task`（§5 规范响应 + 设备校验 + 会话唯一活跃 Task 守卫）
   - `get_task`（§6 Snapshot，非 Store 行）
   - `send_message`（§7 受理：`accepted/task_id/message_id/event_sequence`；Kernel 记 `UserMessageReceived` actor=USER，不动状态机）
   - `control`（§9 pause/resume/cancel/takeover → `kernel.apply_control`）
   - `send_conversation_message`（§8：0 活跃 → 新建 Task（需 device_id，`created: true`）；1 活跃 → 附着（`created: false`）；2+ → 冲突）
   - 幂等操作：完整请求体作为 payload 参与 hash；**重放跳过再校验**（设备后续不可用仍返回原存储响应）
   - 错误映射：`RecordNotFound`→`TASK_NOT_FOUND`、`InvalidControlTransition`→`TASK_NOT_ACTIVE`、`ControlError`→`VALIDATION_ERROR`

**Kernel 公开面新增（Gateway 依赖，均为公共服务）**:
- `list_tasks_by_conversation(conversation_id)`（端口 + SQLite 实现 + Kernel 门面）
- `record_user_message(*, task_id, message_id, conversation_id, text) -> RuntimeEvent`
- `verified_facts(task_id) -> tuple[Fact, ...]`

**测试**（59 passed）:
- `test_gateway_task_gateway.py`（43）：§5 规范响应逐字段、幂等重放/冲突、空白字段、设备三态、会话唯一活跃、Snapshot 逐字段（含完整 action→verify 管道提交的 Fact 投影 + 阶段投影 + 事件末序）、§7 受理与事件落库、§9 四命令序列/非法迁移/未知命令、§8 三种会话路径 + 终态后新建 + 重放跳过再校验
- `test_gateway_idempotency.py`（16）：规范化 hash 稳定性、Store 往返/scope 隔离/初始化三态/重复 put、execute_once 恰好一次/逐字重放/冲突/失败不落库、§14 十码表逐码断言（code + retryable + to_dict 形状）

**全量回归**: **613 passed**（2026-08-20，~131s；基线 554 + 59）

**下一步（Week 4）**: SSE 事件流（`/events/stream`，`after_sequence` 续传 + 心跳）+ 前端控制 UI；`AdbDeviceRegistry` 的 `active_device_ids` overlay 在 cutover 工单接入 Kernel Lease

---

### Phase 6 Week 3: HTTP API 表面 + 错误模型 ✅（2026-08-20, 59 tests）

**目标**: 契约 §2-§14 的规范 HTTP 路由面 —— 独立 router、显式挂载（默认关闭）、§14 十码统一错误渲染；legacy 路由零改动。

**新增文件**:

1. **`backend/ai_game_console/gateway_api.py`**
   - `create_gateway_router(kernel, gateway, device_registry)` → `APIRouter(prefix="/api/v1")`，9 个规范端点:
     `GET /devices`、`POST /tasks`、`GET /tasks`、`GET /tasks/{task_id}`、
     `POST /tasks/{task_id}/messages`、`POST /tasks/{task_id}/controls`、
     `GET /tasks/{task_id}/events`（`after_sequence` 默认 0 + `limit` 默认 100/上限 500 分页，
     严格升序不重复，`next_after_sequence` = 末条 sequence，空页回显游标）、
     `GET /tasks/{task_id}/observations/{observation_id}`（§12 八键投影）、
     `POST /conversations/{conversation_id}/messages`（§8：0 活跃 → 新建 Task 需 `device_id`，
     `created: true`；1 活跃 → 附着 `created: false`；2+ → `CONVERSATION_CONFLICT`）
   - **请求体手动解析**（`_json_object_body`）: 任何解析错误/非对象 → `VALIDATION_ERROR`（400），
     不产生 Pydantic 422 —— 与 legacy `RequestValidationError` 对 `/api/v1/tasks*` 的特殊处理完全解耦
   - **`_guarded`**: 非 `GatewayError` 异常归一为 `INTERNAL_ERROR`（500），traceback/供应商原始错误不外泄
   - **`GATEWAY_ERROR_STATUS`**: 十码 → HTTP 显式映射（400 VALIDATION/CURSOR、404 NOT_FOUND 族、
     409 CONFLICT/NOT_ACTIVE/NOT_AVAILABLE、403 LEGACY_TASK_WRITE_DISABLED、500 INTERNAL）；
     契约未规定状态码，成功响应一律 200
   - `GatewayError` → `{"error": {code, message, retryable, details}}` 异常处理器
   - **`GatewayComposition`**（frozen dataclass: kernel/gateway/device_registry/store）
     + `build_gateway_composition(*, settings, ...)` 工厂（缺省组装 Kernel+Gateway+`AdbDeviceRegistry`）
2. **`backend/ai_game_console/runtime_adapters/android/device_registry.py`**
   - `AdbDeviceRegistry(discovery, *, active_device_ids=None)`: READY → `available`、
     active 集合（预留 cutover Lease overlay）→ `in_use`、其余 → `unavailable`；
     设备 id 约定 `adb:{serial}`；`GET /devices` 仅投影 `{"id", "availability"}`
3. **`api.py` 挂载**（5 处显式改动，默认零影响）
   - `create_app(..., gateway: GatewayComposition | None = None)` 新参数
   - 启用时: `app.state.gateway` + `GatewayError` 异常处理器 + gateway router 在 legacy router
     **之前**注册（共享 canonical 路径按注册顺序解析到 gateway —— 预览 cutover）
   - lifespan 追加 `gateway.store.close` → `gateway.kernel.close`（有专项测试）

**测试**（`tests/backend/test_gateway_api.py`, 59 passed）:
- 默认 app（无 gateway）: 规范路由不存在 + 404 精确错误体 + 现有行为不变
- `GET /devices` 三态投影；未知设备 404 `DEVICE_NOT_FOUND`
- create/get 规范形状逐字段（§5 六键 / §6 十一键）+ 幂等重放 + `Idempotency-Key` 缺失 400
- message（§7 四键受理 + user 事件落库 + 错误会话 409 + 终态 409 + 重放不重复事件）
- control（§9 四命令形状 + takeover → `PAUSED`/`UserTakeover` + 非法迁移 409）
- events 分页（after_sequence/limit/空页游标/未知 task 404/actor 小写）
- observation 八键投影 + 404
- 会话入口 §8 三路径 + `created` 标志 + `X-Client-Id` 要求
- 写端点无 `X-AI-Game-Client` → 403 精确响应体；未知 `/api/*` → 404 精确错误体
- gateway 对共享 canonical 路径的**行为级**优先级证明（POST /tasks → gateway 六键响应）
- 非 `GatewayError` → 500 `INTERNAL_ERROR`（异常信息不外泄）；lifespan 关闭顺序

**契约 §17 覆盖**: 第 1–5 项（create/get、message/control、会话唯一关联、幂等、event 分页）通过；
第 6 项 SSE 断线续传按计划属 Week 4。

**全量回归**: **672 passed**（2026-08-20，~161s；基线 613 + 59）

---

### Phase 6 Week 4: SSE 事件流 + 客户端投影 + 控制 UI ✅（2026-08-20, 后端 7 tests + 前端 13 tests）

**目标**: 契约 §11 SSE 流（`after_sequence` 续传 + 心跳）+ 客户端事件投影（§13）+ 控制 UI；§17 第 6–7 项验收。

**后端 — `gateway_api.py` 新增 `GET /api/v1/tasks/{task_id}/events/stream`**:
- 冻结帧形: `id: <sequence>` + `event: runtime_event` + `data: {"sequence", "type", "payload"}`（严格三键）+ 空行
- 断线续传: 客户端断开后带最后收到的 sequence 重连，服务端从该游标之后继续投递（不依赖 Last-Event-ID 头）
- 心跳: `: heartbeat` 注释帧（每 `heartbeat_interval` 秒，不占 Task sequence）
- 游标校验: 非整数/负数 → 400 `EVENT_CURSOR_INVALID`；未知 task → 404 `TASK_NOT_FOUND`（均在流开启前）
- 响应头: `text/event-stream` + `no-cache`
- **测试走真服务器**: starlette TestClient 阻塞其 transport 直到 ASGI app 调用完成，而 §11 流永不完成 ——
  故 `test_gateway_sse.py` 用 uvicorn（daemon 线程 + 随机空闲端口）走真实 socket；
  所有任务活动都经 HTTP 表面（create → message → cancel），Kernel 写入全部落在服务器线程

**前端 — SSE 客户端 + 投影 + 控制 UI**:
1. **`src/gateway/eventStream.ts`**: 浏览器 EventSource 的自动重连不可用（服务端忽略 Last-Event-ID 头），
   客户端自持游标：断线后 `connect(afterSequence)` 显式带 `after_sequence` 重开；
   游标单调递增（`sequence <= cursor` 的帧被丢弃），`connect()` 永远只能推进游标、不能回退；
   心跳注释帧到不了 message 监听器；`parseSseData()` 严格校验冻结三键帧，畸形帧静默忽略；
   `calibrateCursor(local, snapshot.last_event_sequence) = max(两者)`（§11 Snapshot/Event 校准，永不回退）
2. **`src/gateway/projection.ts`**（§13 投影规则）: 每个 Runtime 事件 → 一行用户可读中文
   （只读事件 payload 中 Kernel 实际发出的字段，缺字段降级、未知类型诚实降级，绝不制造
   Runtime 中不存在的 Stage/Fact/完成状态）；`isUserTakeover(events)`: Kernel 把 pause 和
   takeover 都映射到 PAUSED，投影用"最新暂停族事件是否为 UserTakeover"区分接管态
3. **`src/gateway/useGatewayTaskStream.ts`**: 每个选中任务的生命周期 ——
   GET §6 Snapshot（状态/阶段/控制按钮）→ 从 sequence 0 经 §10 分页逐页回填完整事件历史 →
   在 `calibrateCursor(回填游标, snapshot.last_event_sequence)` 处打开 §11 SSE →
   断线: 重新 GET Snapshot + 校准 + 回填缺口 + 固定延迟重连（上限 8 次，可见"流已断开/重连中"）→
   终态任务不开流
4. **`src/components/GatewayTaskWorkspace.tsx`**: 任务卡片状态徽标 +
   pause/resume/cancel/takeover 四个控制按钮（按 Snapshot `status` 可用性启用，
   终态全禁）；takeover 态显示醒目横幅"用户接管中"（warning 色状态徽标）；
   SSE 帧经投影模块变为 `task_progress` 用户可读消息，追加到"任务进展"时间线；
   流健康显示（已连接/重连中/已断开 n 次）；控制命令携带 `Idempotency-Key`
5. **`src/status.ts`**: `gatewayTaskStatusMeta(status, { userTakeover })` ——
   PAUSED + 接管 → "用户接管中 / 用户已接管设备操作，任务保持暂停"（warning）

**测试**:
- 后端 `tests/backend/test_gateway_sse.py`（7 tests，全走真 uvicorn 服务器）:
  - 冻结帧形 + 三键 payload 形状
  - 响应头 `text/event-stream` + `no-cache`
  - **断线续传**: 断开 → 带最后游标重连 → 只收到新事件，无重复无丢失
  - 从 0 重连回放完整日志
  - 心跳是注释帧、不占 sequence（下一真实事件编号连续）
  - 未知 task → 404 `TASK_NOT_FOUND`（流开启前）
  - 非法游标 → 400 `EVENT_CURSOR_INVALID`（流开启前）
- 前端 `tests/frontend/`（13 tests，全部通过）:
  - `gatewayEventStream.test.ts`（8）: 游标去重、connect 只推进不回退、非法游标拒绝、
    断线 onDrop + 按游标重开、畸形帧忽略、teardown 后无回调、parseSseData 三键校验、calibrateCursor max
  - `gatewayProjection.test.ts`（5）: 已知事件 → 可读文本、未知类型诚实降级、长字段截断、
    字段透传、takeover 判定仅凭最新暂停族事件
- 前端全量: **52 passed**（9 文件）；`App.test.tsx` 导航断言同步加入"网关任务"
  （Week 3 挂载项，旧断言未覆盖已修正）
- **前端构建**: `tsc --noEmit && vite build` 通过（dist: index-*.js 286 kB / gzip 87 kB）

**契约 §17 覆盖**: 第 6 项（SSE 断线续传）+ 第 7 项（Snapshot + Event 校准）通过。

**全量回归**: **679 passed**（2026-08-20，~167s；基线 672 + 7 SSE 测试）

**文档**:
- `PHASE_6_WEEK_4_SSE_CLIENT_UI.md`

### Phase 6 集成: §17 全清单验收 + 全量回归 ✅（2026-08-20, 新增 9 tests）

**契约 §17 十项逐项核对 — 10/10 通过**（证据见 `PHASE_6_INTEGRATION_ACCEPTANCE.md`）:

| # | 契约项 | 证据 |
|---|---|---|
| 1 | Task create/get | Week 3 `test_gateway_api.py` `TestCreateTask`/`TestGetTask` |
| 2 | message/control | Week 3 `TestMessages`/`TestControls` |
| 3 | conversation 唯一关联 | Week 3 `TestConversations` |
| 4 | 幂等 create/message/control | Week 3 + `test_gateway_idempotency.py` |
| 5 | Event sequence 与分页 | Week 3 `TestEvents` |
| 6 | SSE 断线续传 | Week 4 `test_gateway_sse.py`（真 uvicorn socket） |
| 7 | Snapshot + Event 校准 | Week 3 snapshot `last_event_sequence` + Week 4 游标重连 |
| 8 | Web/Hermes 共享语义 | 集成 `test_gateway_integration.py` `TestSharedSemantics`（web/hermes/wechat 逐字节一致） |
| 9 | Client 无法访问 ADB | 集成 `TestNoAdbSurface`（路由集 == §2 canonical 集、无 ADB 端点、错误信封无 ADB 细节） |
| 10 | legacy write 不绕过 Kernel | 集成 `TestLegacyWritesCannotBypassKernel`（canonical 路径全由 Gateway 服务并落 Kernel） |

**场景 10 真机冒烟**: **待设备** —— 本机无 adb（PATH 与常见 SDK 位置均无 `adb.exe`）；
Fake 证据（Fake Device Registry + Fake Observation Provider 跑完整 create→message→
control→events→SSE 链路）已由 Week 3/4 + 集成测试覆盖。

**契约冻结保持**: 本阶段未新增/修改 API；`create_app` 默认 gateway OFF（默认组合与
legacy 逐字节一致）；`LEGACY_TASK_WRITE_DISABLED` 仍为预留码（未 raise），由 cutover
工单在禁用 legacy 写时启用。

**全量回归**: **688 passed**（后端，~170s；基线 679 + 9 集成）+ 前端 **52 passed**
（9 文件）+ 前端构建绿。

**文档**:
- `PHASE_6_INTEGRATION_ACCEPTANCE.md`

### Phase 7: Legacy 迁移 ✅（2026-08-20, 新增 20 tests）

**三阶段运行时模式切换**（`AI_GAME_RUNTIME_MODE`：`legacy` / `draining` / `kernel_active`，默认 `legacy`，非法值启动 fail-fast）：
- Week 1 ✅ `runtime_mode.py`（`validate_runtime_mode` + `RuntimeModeGuard`）+ `create_app` 接线（`app.state.runtime_mode_guard`）+ 门控 `POST /tasks`（`require_legacy_writable`）、`/inputs`、`/stop`（`require_legacy_runtime_available`，仅 KERNEL_ACTIVE 拒绝）+ `LEGACY_TASK_WRITE_DISABLED` 403 + `GET /events` `Deprecation: true`
- Week 2 ✅ 切流前快照（`legacy_cutover.py::snapshot_legacy_mobile_tasks`，SQLite 在线备份 + 缺失占位）+ 排空门禁（`active_tasks()` 在途存量 `queued/planning/running/stopping`，`drain_gate_satisfied`）
- Week 3 ✅ `GET /runtime/mode` 监控端点（`RuntimeModeResponse`）+ 被拒写 warning 日志（端点 + 模式 + 原因）+ 回滚 runbook + 切流/回滚序列 + 快照恢复校验

**计划 §6 七项验收 — 7/7 通过**（证据见 `PHASE_7_INTEGRATION_ACCEPTANCE.md`）:

| # | 验收项 | 证据 |
|---|---|---|
| 1 | 三模式×端点矩阵 | `test_legacy_cutover.py`（legacy 全 202 / draining 创建 403·存量 202 / kernel_active 三写 403·只读 200） |
| 2 | `LEGACY_TASK_WRITE_DISABLED` 403 正确启用 | 同上矩阵（403 + `error.code` 断言） |
| 3 | `GET /events` `Deprecation: true` | `test_events_deprecation_header_in_all_modes`（三模式） |
| 4 | 快照可生成可恢复 | `test_legacy_cutover_helpers.py`（一致性备份 / 缺失占位 / 坏写→覆盖恢复逐行一致） |
| 5 | `GET /runtime/mode` 模式 + 在途存量 | `test_legacy_cutover_integration.py`（三模式布尔 + 排空推进 2→0） |
| 6 | KERNEL_ACTIVE→LEGACY 回滚后 Legacy 写恢复 | `test_cutover_and_rollback_sequence`（`POST /tasks` 202→403→403→202） |
| 7 | 全量回归绿 + 验收文档 + roadmap | 见下 + `PHASE_7_INTEGRATION_ACCEPTANCE.md` + 本 roadmap |

**非破坏性 / 边界**: 模式只改「Legacy 写路径是否开放」，不删数据；回滚 = 配置回退 +（按需）快照恢复；Gateway 挂载保持默认 OFF（不混合挂载）；只读归档任意模式恒 200。

**全量回归**: **708 passed**（后端，~176s；基线 688 + 20）+ 前端 **52 passed** + 前端构建绿。

**文档**:
- `PHASE_7_LEGACY_CUTOVER_PLAN.md`
- `PHASE_7_ROLLBACK_RUNBOOK.md`
- `PHASE_7_INTEGRATION_ACCEPTANCE.md`

---

## 🔄 后续阶段（Phase 6-7）

### Phase 6: Gateway 契约实现

**目标**: 实现 Client ↔ Gateway ↔ Kernel 消息路由（契约 §1-§17）

**核心内容**:
- Week 1 ✅ Kernel 控制面（见上节）
- Week 2 ✅ Gateway 应用服务（会话、幂等、Snapshot 投影，59 tests，见上节）
- Week 3 ✅ HTTP API 表面 + 10 码错误渲染 + 显式挂载（59 tests，见上节）
- Week 4 ✅ SSE 流 + 客户端投影 + 控制 UI（见上节）
- 集成 ✅ 场景 10 真机冒烟（当前机器无 adb，记录"待设备"）+ §17 全清单 10/10 验收 + 全量回归（见 `PHASE_6_INTEGRATION_ACCEPTANCE.md`）

**预估时间**: 4 周 + 集成（详见 `PHASE_6_GATEWAY_CONTRACT_PLAN.md`）

---

### Phase 7: Legacy 迁移 ✅（2026-08-20）

**目标**: 三阶段平滑切换，零停机迁移

**阶段**:
1. LEGACY_ACTIVE: 新旧并存（新功能走 Kernel）
2. DRAINING: 拒绝新 Legacy Task，等待存量完成
3. KERNEL_ACTIVE: 完全禁用 Legacy API

**核心内容**:
- Legacy Task 数据迁移
- 运行时模式切换
- 监控和回滚机制

**交付**:
- Week 1 ✅ 运行时模式 guard（`runtime_mode.py`：`validate_runtime_mode` + `RuntimeModeGuard`）+ `create_app` 接线 + 门控 `POST /tasks`、`/inputs`、`/stop`（`LEGACY_TASK_WRITE_DISABLED` 403）+ `GET /events` `Deprecation: true` + 三模式×端点矩阵测试
- Week 2 ✅ 切流前快照（`legacy_cutover.py::snapshot_legacy_mobile_tasks`，SQLite 在线备份 + 缺失占位）+ 排空门禁（`active_tasks()` 在途存量 `queued/planning/running/stopping`）+ 测试
- Week 3 ✅ `GET /runtime/mode` 监控端点（`RuntimeModeResponse`）+ 被拒写 warning 日志 + 回滚 runbook + 切流/回滚序列 + 快照恢复校验测试
- 集成 ✅ §6 七项验收 7/7 + 全量回归（后端 708 / 前端 52 / build 绿）（见 `PHASE_7_INTEGRATION_ACCEPTANCE.md`、`PHASE_7_ROLLBACK_RUNBOOK.md`）

**预估时间**: 2-3 周（实际 2026-08-20 同日完成）

---

## 📈 项目进度

### 整体进度
- **已完成**: Phase 0-7 全部完成；Phase 6 Week 1（Kernel 控制面）+ Week 2（Gateway 应用服务包）+ Week 3（HTTP API 表面 + 错误模型）+ Week 4（SSE 事件流 + 客户端投影 + 控制 UI）+ 集成（§17 全清单 10/10 验收、场景 10 待设备、全量回归，2026-08-20）；Phase 7 Legacy 迁移 Week 1（运行时模式 guard + 接线 + 门控 + Deprecation）+ Week 2（快照 + 排空门禁）+ Week 3（监控端点 + 回滚 runbook + 切流/回滚/快照恢复测试）+ 集成（§6 七项验收 7/7、全量回归，2026-08-20）
- **待开始**: —（路线图 Phase 0-7 全部完成）

### 代码统计（截至 Phase 7 集成, 2026-08-20）
- 后端测试: 708 passed（Phase 6 基线 688 + Phase 7 新增 20：`test_runtime_mode.py` +2 + `test_legacy_cutover.py` 7 + `test_legacy_cutover_helpers.py` 6 + `test_legacy_cutover_integration.py` 5）
- 前端测试: 52 passed（9 文件，本阶段无前端改动，与 Phase 6 一致）
- 代码行数: ~18,500 lines (backend runtime_kernel + gateway 包 + gateway_api + device_registry + `runtime_mode`/`legacy_cutover` + 前端 gateway 客户端)
- 文档: 18+ 设计文档（新增 `PHASE_7_LEGACY_CUTOVER_PLAN.md` + `PHASE_7_ROLLBACK_RUNBOOK.md` + `PHASE_7_INTEGRATION_ACCEPTANCE.md`）

### 时间估算
- Phase 5: 已完成（剩余 0）
- Phase 6: 已完成（Week 1-4 + 集成，2026-08-20 同日完成）
- Phase 7: 已完成（Week 1-3 + 集成，2026-08-20 同日完成）
- **总计剩余**: 0（路线图 Phase 0-7 全部完成）

---

## 💡 关键设计决策

### 1. 隔离边界
- Runtime Kernel 完全独立于 FastAPI/Frontend
- 通过 Port/Adapter 模式隔离依赖
- 测试使用 Fake 实现，不依赖外部服务

### 2. 原子性恢复
- 使用 `unresolved_action_ref` 阻止重放
- UNCERTAIN verdict 强制人工介入
- Checkpoint 快照隔离和去重

### 3. 设备独占
- UNIQUE(device_id) 数据库约束
- Lease TTL + 自动续期
- Deadline 防止无限续期

### 4. 渐进式迁移
- 三阶段模式切换（LEGACY → DRAINING → KERNEL_ACTIVE）
- 运行时模式守卫
- 新旧系统共存期间的兼容性

---

## 🚀 下一步行动

### Phase 5 已完成（2026-08-19 全部收官）
- ✅ Week 7：20 个测试全部通过，测试报告 + 故障排查指南（首版）+ 代码审查 → `docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md`
- ✅ Week 5 补做：Deadline 保护（10 tests）→ `docs/NEW/PHASE_5_WEEK_5_DEADLINE_PROTECTION.md`
- ✅ Week 6 补做：管理 UI/API（9 tests）→ `docs/NEW/PHASE_5_WEEK_6_ADMIN_API.md`
- ✅ 全量回归 529 passed
- 场景 10 真机冒烟并入 Phase 6 集成阶段（当前机器无 adb）

### Phase 6（2026-08-20 启动，2026-08-20 完成）
- ✅ Week 1：Kernel 控制面（25 tests，全量回归 554 passed）
- ✅ Week 2：`gateway/` 应用服务包（8 模块，59 tests，全量回归 613 passed）—— §14 错误表、GatewayStore、§3 幂等、设备注册表端口、ConversationService（§8）、Snapshot 投影（§6）、TaskGateway（§5/§7/§9）
- ✅ Week 3：`gateway_api.py` HTTP 表面（59 tests，全量回归 672 passed）—— 9 个规范端点、§14 十码错误渲染（400/404/409/403/500 显式映射）、`AdbDeviceRegistry`（`adb:{serial}`）、`create_app(gateway=...)` 显式挂载（默认关闭，启用时先于 legacy 注册）、§17 第 1–5 项通过
- ✅ Week 4：SSE 事件流 + 客户端投影 + 控制 UI（后端 7 SSE tests + 前端 13 tests，全量回归 679 passed）—— `GET /tasks/{task_id}/events/stream`（`after_sequence` 续传 + 心跳 + 流前校验）、`eventStream.ts`（自持游标、断线重开、`calibrateCursor` max 校准）、`projection.ts`（§13 用户可读投影 + takeover 判定）、`useGatewayTaskStream.ts`（Snapshot→回填→SSE→断线重连）、`GatewayTaskWorkspace.tsx`（pause/resume/cancel/takeover 按钮 + takeover 横幅 + 任务进展时间线）
- ✅ **集成（完成）**：契约 §17 全清单 **10/10** 逐项核对通过 + 场景 10 真机冒烟（当前机器无 adb，记录"待设备"，Fake 证据覆盖全链路）+ 全量回归（后端 **688 passed** + 前端 **52 passed**）→ `PHASE_6_INTEGRATION_ACCEPTANCE.md`

### 立即开始
1. **Phase 7: Legacy 迁移**（三阶段切换、数据迁移）

### 中期规划（Phase 7+）
1. Legacy 迁移策略（LEGACY_ACTIVE → DRAINING → KERNEL_ACTIVE）
2. 生产环境部署

---

## 📚 参考文档

### 设计文档（docs/NEW/）
- `PHASE_0_CURRENT_BASELINE.md`: 现有代码分析
- `PHASE_1_RUNTIME_KERNEL_BOUNDARY_DESIGN.md`: 边界设计
- `PHASE_1_DEVICE_OWNERSHIP.md`: 设备所有权不变量
- `PHASE_1_DATA_MODEL_DESIGN.md`: 数据模型
- `PHASE_2_RUNTIME_PERSISTENT_SPINE.md`: 持久化脊柱
- `PHASE_3_OBSERVATION_SPINE.md`: Observation 脊柱
- `PHASE_4_ACTION_VERIFY_COMMIT_SPINE.md`: Action 脊柱
- `PHASE_4_RECOVERY_STRATEGY.md`: 恢复策略
- `PHASE_5_DEVICE_OWNERSHIP_IMPL.md`: 设备所有权实施
- `PHASE_1_GATEWAY_CONTRACT_DESIGN.md`: Gateway 契约（DESIGN FROZEN，Phase 6 依据）

### 实施总结
- `PHASE_5_WEEK_4_DEVICE_LEASE_MANAGER.md`: Week 4 实施总结
- `PHASE_5_WEEK_5_DEADLINE_PROTECTION.md`: Week 5 Deadline 保护（绝对截止 + 强制回收）
- `PHASE_5_WEEK_6_ADMIN_API.md`: Week 6 管理 API 规范 + 运维手册
- `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`: Week 7 端到端集成测试计划与进度
- `PHASE_5_WEEK_7_E2E_VERIFICATION.md`: Week 7 测试报告 + 故障排查指南 + 代码审查
- `PHASE_6_GATEWAY_CONTRACT_PLAN.md`: Phase 6 实施计划（4 周 + 集成）
- `PHASE_6_WEEK_4_SSE_CLIENT_UI.md`: Week 4 SSE 事件流 + 客户端投影 + 控制 UI 实施总结

---

**最后更新**: Phase 6 集成完成（§17 全清单 10/10 验收、场景 10 待设备, 2026-08-20），全量回归 688 passed（后端）+ 前端 52 passed；Phase 6 ✅，进入 Phase 7
