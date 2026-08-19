# Phase 5 Week 7: 端到端集成测试计划

**目标**: 验证 DeviceExecutionLease 完整生命周期和恢复流程  
**状态**: ✅ 完成（Day 1-5 全部完成，2026-08-19 收官）  
**预计时间**: 3-5 天

---

## 📋 测试场景清单

### 1. 并发冲突测试 ✅

**场景**: 两个进程同时尝试 acquire 同一设备

**验证点**:
- ✅ 只有一个进程获取成功
- ✅ 另一个进程抛出 `LeaseConflict`
- ✅ 数据库 UNIQUE 约束生效
- ✅ 失败方不影响成功方的 Lease

**实现方式**:
- 多进程测试（subprocess）
- 或多线程测试（threading）

---

### 2. Lease 正常生命周期测试 ✅

**场景**: 完整的 acquire → renew → release 流程

**验证点**:
- ✅ acquire 成功获取 Lease
- ✅ renew 成功延长过期时间
- ✅ release 成功释放设备
- ✅ 释放后其他进程可以获取
- ✅ execute_action 正确集成 Lease

**已有测试**: `test_runtime_kernel_execute_action.py` 部分覆盖

---

### 3. 进程崩溃恢复测试 ✅ (Day 1)

**场景**: 进程在持有 Lease 时崩溃

**验证点**:
- ✅ 后台清理检测到孤立 Lease
- ✅ 为 PROPOSED/EXECUTED Action 创建 Checkpoint
- ✅ Checkpoint 包含 unresolved_action_ref
- ✅ 孤立 Lease 被正确释放
- ✅ Task 进入可恢复状态

**测试**: `test_runtime_kernel_e2e_integration.py::test_orphaned_lease_recovery_integration`

**实现方式**:
- 模拟进程死亡（修改 process_id 为不存在的 PID）
- 触发后台清理
- 验证 Checkpoint 创建

---

### 4. Lease 过期清理测试 ✅ (Day 1)

**场景**: Lease 超过 TTL 但进程仍存活

**验证点**:
- ✅ 后台清理检测到过期 Lease
- ✅ 记录警告日志（进程存活但 Lease 过期）
- ✅ Lease 被释放
- ✅ 如果有关联 Action，创建 Checkpoint

**测试**: `test_runtime_kernel_e2e_integration.py::test_expired_lease_cleanup_integration`

**实现方式**:
- 创建过期的 Lease（expires_at < now）
- 使用真实进程 PID（当前进程）
- 触发后台清理

---

### 5. 多 Task 并发执行测试 ✅ (Day 2)

**场景**: 多个 Task 同时执行在不同设备

**验证点**:
- ✅ 每个 Task 获取各自设备的 Lease
- ✅ 不同设备的 Lease 互不干扰
- ✅ 同时执行 Action 不冲突
- ✅ 所有 Lease 正确释放

**测试**: `test_runtime_kernel_e2e_concurrency.py::test_multi_task_concurrent_execution_on_different_devices`

**实现方式**:
- 创建多个 Task，绑定不同 device_id
- 并发执行 Action
- 验证 Lease 隔离

---

### 6. 后台清理线程稳定性测试 ✅ (Day 2 + Day 4)

**场景**: 后台清理线程长期运行

**验证点**:
- ✅ 线程启动和停止正常
- ✅ 清理循环不会崩溃
- ✅ 异常被正确捕获和记录
- ✅ CPU 占用合理（<1%）：实测 **0.16%**（Day 4，空闲、10s 窗口）

**测试**: `test_runtime_kernel_e2e_concurrency.py::test_background_cleanup_thread_stability` + `test_background_cleanup_handles_exceptions_gracefully` + `test_runtime_kernel_e2e_performance.py::test_background_cleanup_cpu_idle`

**实现方式**:
- 启动后台清理
- 创建多个过期/孤立 Lease
- 运行 10-20 次清理循环
- 监控异常和性能

---

### 7. Checkpoint 去重测试 ✅ (Day 1)

**场景**: 同一个孤立 Lease 被多次扫描

**验证点**:
- ✅ 第一次扫描创建 Checkpoint
- ✅ 后续扫描检测到已存在的 Checkpoint
- ✅ 不创建重复 Checkpoint
- ✅ 日志记录跳过原因

**测试**: `test_runtime_kernel_e2e_integration.py::test_checkpoint_deduplication_on_repeated_cleanup`

**实现方式**:
- 创建孤立 Lease
- 触发清理（创建 Checkpoint）
- 再次触发清理
- 验证 Checkpoint 数量不变

---

### 8. 跨平台进程检测测试 ✅

**场景**: 验证 Windows + Unix 进程检测逻辑

**验证点**:
- ✅ 当前进程 PID 检测为存活（Day 1 过期清理测试使用真实存活 PID）
- ✅ 不存在的 PID (999999) 检测为不存在（Day 1 孤立 Lease 测试使用假 PID）
- ✅ 无效 PID ("invalid") 返回 False（单元测试）
- ✅ PermissionError 正确处理（Unix，单元测试）

**实现方式**:
- 单元测试已覆盖 (`test_device_lease_manager.py`)
- Day 1/2 集成测试验证了实际行为（存活 PID 走过期路径、死 PID 走孤立路径）

---

### 9. execute_action 完整流程测试 ✅ (Day 3)

**场景**: propose → execute → verify → commit 完整链路

**验证点**:
- ✅ propose_action 创建 PROPOSED Action
- ✅ execute_action 获取 Lease
- ✅ 类型化 Executor 执行成功（5 种类型全覆盖：tap/swipe/input_text/back/home）
- ✅ execution 记录正确（`ActionExecution.lease_ref` 非空、EXECUTED 状态）
- ✅ Lease 自动释放（`get_lease_for_device` 返回 None）
- ✅ verify_action 和 commit 正常（SUCCESS 提交 Facts + Stage 推进 + Checkpoint；FAIL 记录失败不阻断恢复）

**实现方式**:
- 完整类型化 FakeExecutor（实现 `ActionExecutorPort` 全部 5 个方法）
- 完整执行 Action 流程，验证每个状态转换

**测试**:
- `test_runtime_kernel_e2e_integration.py::test_execute_action_complete_flow_integration`（SUCCESS + 提交）
- `test_runtime_kernel_e2e_integration.py::test_execute_action_complete_flow_fail_verdict_integration`（FAIL + 恢复）
- `test_runtime_kernel_execute_action.py`：INPUT_TEXT/HOME 分发、Lease 冲突（`LeaseConflict`）、重放拒绝、过期决策拒绝

---

### 10. 真实设备测试 🎯（可选）

**场景**: 在真实 Android 设备上执行 Action

**验证点**:
- [ ] 连接真实设备（adb devices）
- [ ] 执行 tap 成功
- [ ] 执行 swipe 成功
- [ ] 执行 back/home 成功
- [ ] Lease 正确管理

**实现方式**:
- 需要真实 Android 设备
- 使用真实 AdbActionExecutor
- 手动或自动化测试

**优先级**: 低（当前可以用 Fake 验证逻辑）

---

## 📝 测试实施顺序

### Day 1: 进程崩溃恢复和过期清理
1. 实现 `test_orphaned_lease_recovery_integration`
2. 实现 `test_expired_lease_cleanup_integration`
3. 实现 `test_checkpoint_deduplication`

### Day 2: 多 Task 并发和后台线程稳定性
4. 实现 `test_multi_task_concurrent_execution`
5. 实现 `test_background_cleanup_stability`

### Day 3: execute_action 完整流程 ✅
6. ✅ 实现 `test_execute_action_complete_flow_integration`（+ FAIL 裁决变体）
7. ✅ 增强现有 `test_runtime_kernel_execute_action.py`（INPUT_TEXT/HOME、Lease 冲突、重放拒绝、过期决策拒绝）

### Day 4: 并发冲突和性能测试 ✅
8. ✅ 实现 `test_concurrent_lease_acquisition_conflict`（实际在 Day 2 完成）
9. ✅ 性能基准测试（Lease 续期开销 + 多设备规模 + 清理线程 CPU）

**Day 4 基准结果**（`test_runtime_kernel_e2e_performance.py`，3 tests）:

| 操作 | n | 中位数 | p99 | max |
|------|---|--------|-----|-----|
| acquire_lease | 200 | 7.9ms | 8.9ms | 10.2ms |
| renew_lease | 600 | 7.9ms | 8.3ms | 10.1ms |
| release_lease | 200 | 7.9ms | 10.2ms | 19.2ms |
| 50 设备并发获取+释放 | 50+50 | 6.4ms | — | 总计 690ms |
| 后台清理线程 CPU（空闲） | 10s 窗口 | **0.16%** | — | — |

注：单操作 ~8ms 主要来自每次调用新建 SQLite 连接的开销（文件打开 + PRAGMA），
对 Lease 操作频率（每 Task 每分钟几次）完全可接受；无 busy-loop、无病态锁等待。

### Day 5: 文档和清理 ✅
10. ✅ 编写测试报告 → `docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md`
11. ✅ 更新故障排查指南 → 同上文档 §8（首版，10 类症状 + SQL 诊断速查）
12. ✅ 代码审查和重构 → 同上文档 §9（4 个测试文件审查通过，无需重构）

---

## 🎯 成功标准

- ✅ 所有新增集成测试通过（实际 20 个：e2e_integration 5 + e2e_concurrency 4 + execute_action 8 + e2e_performance 3）
- ✅ 全量回归继续通过（实际 510 passed，2026-08-19）
- ✅ 无内存泄漏或资源泄漏（Day 5 报告 §6 资源泄漏检查：连接即开即关、线程 join 确认、tmp_path 零残留）
- ✅ CPU 占用合理（后台清理实测 0.16% < 1%）
- ✅ 文档完整（`docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md`：测试报告 + 故障排查指南 + 代码审查）

---

## 📚 参考资料

- `test_runtime_kernel_lease.py`: Lease 基础测试
- `test_device_lease_manager.py`: LeaseManager 单元测试
- `test_runtime_kernel_execute_action.py`: execute_action 测试
- `test_runtime_kernel_recovery_paths.py`: 恢复路径测试

---

**开始时间**: 2026-08-18  
**实际完成**: 2026-08-19（提前一天，Day 5 报告/指南/审查一次交付）

---

## 📅 进度记录

- **Day 1** (commit b1f1810): 进程崩溃恢复、Lease 过期清理、Checkpoint 去重 — 3 个集成测试 ✅
- **Day 2** (commit 6a508cd): 多 Task 并发、并发 acquire 冲突、后台清理线程稳定性与异常容错 — 4 个集成测试 ✅
- **Day 3** (2026-08-19): execute_action 完整流程（SUCCESS 提交 + FAIL 恢复）、execute_action 测试增强（5 种类型全覆盖、LeaseConflict、防重放、防过期决策）— 6 个新测试 ✅
- **Day 4** (2026-08-19): 性能基准 — acquire/renew/release 中位数 ~7.9ms、50 设备并发 690ms、后台清理空闲 CPU 0.16%（<1% 达标）— 3 个新测试 ✅，全量回归 510 passed
- **Day 5** (2026-08-19): 测试报告 + 故障排查指南（首版）+ 代码审查 — `docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md` ✅，Week 7 收官
