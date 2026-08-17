# Phase 5 Week 7: 端到端集成测试计划

**目标**: 验证 DeviceExecutionLease 完整生命周期和恢复流程  
**状态**: 🚧 进行中  
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

### 3. 进程崩溃恢复测试 🚧

**场景**: 进程在持有 Lease 时崩溃

**验证点**:
- [ ] 后台清理检测到孤立 Lease
- [ ] 为 PROPOSED/EXECUTED Action 创建 Checkpoint
- [ ] Checkpoint 包含 unresolved_action_ref
- [ ] 孤立 Lease 被正确释放
- [ ] Task 进入可恢复状态

**实现方式**:
- 模拟进程死亡（修改 process_id 为不存在的 PID）
- 触发后台清理
- 验证 Checkpoint 创建

---

### 4. Lease 过期清理测试 🚧

**场景**: Lease 超过 TTL 但进程仍存活

**验证点**:
- [ ] 后台清理检测到过期 Lease
- [ ] 记录警告日志（进程存活但 Lease 过期）
- [ ] Lease 被释放
- [ ] 如果有关联 Action，创建 Checkpoint

**实现方式**:
- 创建过期的 Lease（expires_at < now）
- 使用真实进程 PID（当前进程）
- 触发后台清理

---

### 5. 多 Task 并发执行测试 🚧

**场景**: 多个 Task 同时执行在不同设备

**验证点**:
- [ ] 每个 Task 获取各自设备的 Lease
- [ ] 不同设备的 Lease 互不干扰
- [ ] 同时执行 Action 不冲突
- [ ] 所有 Lease 正确释放

**实现方式**:
- 创建多个 Task，绑定不同 device_id
- 并发执行 Action
- 验证 Lease 隔离

---

### 6. 后台清理线程稳定性测试 🚧

**场景**: 后台清理线程长期运行

**验证点**:
- [ ] 线程启动和停止正常
- [ ] 清理循环不会崩溃
- [ ] 异常被正确捕获和记录
- [ ] CPU 占用合理（<1%）

**实现方式**:
- 启动后台清理
- 创建多个过期/孤立 Lease
- 运行 10-20 次清理循环
- 监控异常和性能

---

### 7. Checkpoint 去重测试 🚧

**场景**: 同一个孤立 Lease 被多次扫描

**验证点**:
- [ ] 第一次扫描创建 Checkpoint
- [ ] 后续扫描检测到已存在的 Checkpoint
- [ ] 不创建重复 Checkpoint
- [ ] 日志记录跳过原因

**实现方式**:
- 创建孤立 Lease
- 触发清理（创建 Checkpoint）
- 再次触发清理
- 验证 Checkpoint 数量不变

---

### 8. 跨平台进程检测测试 🚧

**场景**: 验证 Windows + Unix 进程检测逻辑

**验证点**:
- [ ] 当前进程 PID 检测为存活
- [ ] 不存在的 PID (999999) 检测为不存在
- [ ] 无效 PID ("invalid") 返回 False
- [ ] PermissionError 正确处理（Unix）

**实现方式**:
- 单元测试已覆盖 (`test_device_lease_manager.py`)
- 集成测试中验证实际行为

---

### 9. execute_action 完整流程测试 🚧

**场景**: propose → execute → verify → commit 完整链路

**验证点**:
- [ ] propose_action 创建 PROPOSED Action
- [ ] execute_action 获取 Lease
- [ ] ADB 执行成功（或失败）
- [ ] execution 记录正确
- [ ] Lease 自动释放
- [ ] verify_action 和 commit 正常

**实现方式**:
- 使用 FakeActionExecutor
- 完整执行一个 Action 流程
- 验证每个状态转换

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

### Day 3: execute_action 完整流程
6. 实现 `test_execute_action_complete_flow_integration`
7. 增强现有 `test_runtime_kernel_execute_action.py`

### Day 4: 并发冲突和性能测试
8. 实现 `test_concurrent_lease_acquisition_conflict`
9. 性能基准测试（Lease 续期开销）

### Day 5: 文档和清理
10. 编写测试报告
11. 更新故障排查指南
12. 代码审查和重构

---

## 🎯 成功标准

- [ ] 所有新增集成测试通过（估计 8-12 个）
- [ ] 现有 510 个测试继续通过
- [ ] 无内存泄漏或资源泄漏
- [ ] CPU 占用合理（后台清理 <1%）
- [ ] 文档完整（测试报告、故障排查）

---

## 📚 参考资料

- `test_runtime_kernel_lease.py`: Lease 基础测试
- `test_device_lease_manager.py`: LeaseManager 单元测试
- `test_runtime_kernel_execute_action.py`: execute_action 测试
- `test_runtime_kernel_recovery_paths.py`: 恢复路径测试

---

**开始时间**: 现在  
**预计完成**: 3-5 天后
