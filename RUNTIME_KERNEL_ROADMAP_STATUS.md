# Runtime Kernel 实施路线图与当前状态

**更新日期**: 2026-08-19  
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
| **Phase 5** | **设备所有权** | **🚧 进行中** | **57 passed** | **Lease、执行器、排空、E2E 集成** |
| Phase 6 | Gateway 契约 | ⏳ 待开始 | - | 消息路由、会话管理 |
| Phase 7 | Legacy 迁移 | ⏳ 待开始 | - | 三阶段切换、数据迁移 |

**当前测试总数**: 507 passed（全量回归，2026-08-19；Phase 5 Week 7 Day 3 后）

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

### Phase 5 Week 7: 端到端集成测试 🚧 (Day 1-3 完成, 17 个新测试)

> 注：原计划的 Week 5（Deadline 保护）和 Week 6（管理 UI/API）暂未实施，
> 项目直接进入 Week 7 端到端集成测试验证已实现的 Lease 生命周期。

**测试文件**:
- `tests/backend/test_runtime_kernel_e2e_integration.py`（5 tests）
- `tests/backend/test_runtime_kernel_e2e_concurrency.py`（4 tests）
- `tests/backend/test_runtime_kernel_execute_action.py`（8 tests，含 Day 3 增强）

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

**全量回归**: 507 passed（~97s）

**文档**:
- `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`（10 个场景，8 个已验证 ✅）

---

## 🚧 Phase 5 剩余工作

### Week 5: Deadline 保护机制 (估计 3-5 天)

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

### Week 6: 管理 UI/API (估计 5-7 天)

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

### Week 7: 端到端集成测试 (3-5 天, 🚧 Day 1-3 完成)

**目标**: 验证完整的 Lease 生命周期和恢复流程

**测试场景**（详见 `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`）:
1. ✅ **并发冲突** (Day 2)：并发 acquire 仅一个成功，另一个 `LeaseConflict`
2. ✅ **进程崩溃恢复** (Day 1)：孤立 Lease 检测 → Checkpoint(unresolved_action_ref)
3. ⏳ **Deadline 超时**：依赖 Week 5（暂未实施）
4. 🎯 **真实设备测试**（可选，低优先级）
5. 🚧 **性能测试** (Day 4 待做)：Lease 续期开销、清理线程 CPU 占用

**剩余** (Day 4-5):
- Day 4: 性能基准（Lease 续期开销、后台清理 CPU <1%）
- Day 5: 测试报告 + 故障排查指南

**文档**:
- 测试报告 (Day 5)
- 性能基准 (Day 4)
- 故障排查指南 (Day 5)

---

## 🎯 Phase 5 成功标准

Phase 5 完成的定义：
- ✅ Week 1-4 完成（40 tests passed）
- 🚧 Week 7 进行中（Day 1-3 完成，17 tests；Day 4 性能基准 / Day 5 报告待做）
- ⏳ Week 5（Deadline 保护）、Week 6（管理 UI/API）——计划保留，暂未实施
- ✅ 所有测试通过（实际：507 passed，2026-08-19）
- ⏳ 真实设备执行 tap/swipe/back/home 成功（可选，低优先级）
- ✅ 两个 Task 无法同时 acquire 同一设备（Day 2 并发冲突测试）
- ✅ Lease 过期后自动清理（Day 1 过期清理测试）
- ✅ 进程崩溃后孤立 Lease 创建恢复 Checkpoint（Day 1 崩溃恢复测试）
- ⏳ Deadline 超时触发自动释放（依赖 Week 5）
- ⏳ 管理 API 可查询和手动干预（依赖 Week 6）
- 🚧 文档完整：Week 7 测试报告/故障排查 Day 5 待做

---

## 🔄 后续阶段（Phase 6-7）

### Phase 6: Gateway 契约实现

**目标**: 实现 Client ↔ Gateway ↔ Kernel 消息路由

**核心内容**:
- Gateway 持久化会话
- 消息路由和分发
- Kernel → Gateway 事件推送
- Client 取消/暂停/恢复 UI

**预估时间**: 3-4 周

---

### Phase 7: Legacy 迁移

**目标**: 三阶段平滑切换，零停机迁移

**阶段**:
1. LEGACY_ACTIVE: 新旧并存（新功能走 Kernel）
2. DRAINING: 拒绝新 Legacy Task，等待存量完成
3. KERNEL_ACTIVE: 完全禁用 Legacy API

**核心内容**:
- Legacy Task 数据迁移
- 运行时模式切换
- 监控和回滚机制

**预估时间**: 2-3 周

---

## 📈 项目进度

### 整体进度
- **已完成**: Phase 0-4 + Phase 5 Week 1-4 + Week 7 Day 1-3
- **进行中**: Phase 5 Week 7（Day 4 性能基准 / Day 5 报告）
- **待开始**: Phase 5 Week 5-6（计划保留）、Phase 6-7

### 代码统计（截至 Week 7 Day 3, 2026-08-19）
- 测试数量: 507 passed（其中 Phase 5 共 57 个，含 Week 7 新增 17 个）
- 代码行数: ~15,000 lines (backend runtime_kernel)
- 文档: 11+ 设计文档

### 时间估算
- Phase 5 剩余: 2-3 周
- Phase 6: 3-4 周
- Phase 7: 2-3 周
- **总计剩余**: 7-10 周

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

### 立即开始（Week 7 Day 4-5）
1. Day 4: 性能基准测试（Lease 续期开销、后台清理 CPU 占用）
2. Day 5: 编写 Week 7 测试报告
3. Day 5: 更新故障排查指南

### 短期规划（Week 5-6，计划保留）
1. Lease Deadline 保护（防止无限续期）
2. 管理 API（Lease 查询与手动干预）

### 中期规划（Phase 6-7）
1. Gateway 消息路由
2. Legacy 迁移策略
3. 生产环境部署

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

### 实施总结
- `PHASE_5_WEEK_4_DEVICE_LEASE_MANAGER.md`: Week 4 实施总结
- `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`: Week 7 端到端集成测试计划与进度

---

**最后更新**: Phase 5 Week 7 Day 3 完成（2026-08-19），全量回归 507 passed
