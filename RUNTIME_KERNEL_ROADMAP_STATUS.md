# Runtime Kernel 实施路线图与当前状态

**更新日期**: 2025-01-XX  
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
| **Phase 5** | **设备所有权** | **🚧 进行中** | **40 passed** | **Lease、执行器、排空** |
| Phase 6 | Gateway 契约 | ⏳ 待开始 | - | 消息路由、会话管理 |
| Phase 7 | Legacy 迁移 | ⏳ 待开始 | - | 三阶段切换、数据迁移 |

**当前测试总数**: 470 → 510 passed (Phase 4 + Phase 5 Week 1-4)

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

### Week 7: 端到端集成测试 (估计 3-5 天)

**目标**: 验证完整的 Lease 生命周期和恢复流程

**测试场景**:
1. **并发冲突**
   - 两个进程同时 acquire 同一设备
   - 验证只有一个成功，另一个抛出 `LeaseConflict`

2. **进程崩溃恢复**
   - 模拟进程在持有 Lease 时崩溃
   - 验证后台清理检测到孤立 Lease
   - 验证 Checkpoint 正确创建

3. **Deadline 超时**
   - 模拟长时间持有 Lease
   - 验证 Deadline 触发释放
   - 验证任务进入恢复状态

4. **真实设备测试**
   - 在真实 Android 设备上执行 Action
   - 验证 Lease 获取/释放
   - 验证 ADB 命令执行

5. **性能测试**
   - Lease 续期开销
   - 清理线程 CPU 占用
   - 大量 Lease 并发场景

**文档**:
- 测试报告
- 性能基准
- 故障排查指南

---

## 🎯 Phase 5 成功标准

Phase 5 完成的定义：
- ✅ Week 1-4 完成（40 tests passed）
- ⏳ Week 5-7 完成（估计新增 15-20 tests）
- ⏳ 所有测试通过（目标：~530 tests）
- ⏳ 真实设备执行 tap/swipe/back/home 成功
- ⏳ 两个 Task 无法同时 acquire 同一设备
- ⏳ Lease 过期后自动清理
- ⏳ 进程崩溃后孤立 Lease 创建恢复 Checkpoint
- ⏳ Deadline 超时触发自动释放
- ⏳ 管理 API 可查询和手动干预
- ⏳ 文档完整：API、配置、部署、故障排查

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
- **已完成**: Phase 0-4 + Phase 5 Week 1-4
- **进行中**: Phase 5 Week 5-7
- **待开始**: Phase 6-7

### 代码统计（截至 Week 4）
- 测试数量: 510 passed
- 代码行数: ~15,000 lines (backend runtime_kernel)
- 文档: 10+ 设计文档

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

### 立即开始（Week 5）
1. 实现 Lease Deadline 保护
2. 扩展 DeviceExecutionLease 模型
3. 修改续期逻辑检查 Deadline
4. 编写 Deadline 超时测试
5. 更新文档

### 短期规划（Week 6-7）
1. 实现管理 API
2. 端到端集成测试
3. 真实设备测试
4. 性能基准测试

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

---

**最后更新**: Phase 5 Week 4 完成，推送到 GitHub (commit ca875c9)
