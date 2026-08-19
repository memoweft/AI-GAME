# Phase 5 Week 7: 端到端集成测试报告

**状态**: ✅ 完成（2026-08-18 ~ 2026-08-19，Day 1-5）  
**关联计划**: `PHASE_5_WEEK_7_E2E_TESTING_PLAN.md`（仓库根目录）  
**关联实现**: `docs/NEW/PHASE_5_DEVICE_OWNERSHIP_IMPL.md`（Week 4 DeviceLeaseManager 设计）

---

## 1. 目标与范围

验证 DeviceExecutionLease 完整生命周期与 execute_action 端到端流程：

- ✅ Lease 生命周期：acquire → renew → release，含并发冲突、TTL 过期、进程崩溃孤立恢复
- ✅ execute_action 完整链路：propose → execute（含 Lease 集成）→ verify → commit，5 种 Action 类型全覆盖
- ✅ 后台清理线程：长期稳定性、异常容错、CPU 占用
- ✅ 性能基准：Lease 单操作开销、50 设备规模、清理线程空闲 CPU
- 🎯 范围外：真实设备测试（场景 10，可选/低优先级）、Week 5 Deadline 保护、Week 6 管理 UI/API（计划保留，暂未实施）

---

## 2. 测试环境

| 项目 | 值 |
|------|-----|
| 平台 | Windows 本地开发机 |
| Python | uv 管理的 venv（`apps/console/backend`） |
| 数据库 | SQLite 文件库（pytest `tmp_path`，每测试隔离，默认 rollback journal） |
| 执行命令 | `uv run --project "F:\AI-GAME\apps\console\backend" pytest "F:\AI-GAME\apps\console\tests\backend" -q` |
| 全量回归 | **510 passed**（118s，2026-08-19） |

---

## 3. 场景覆盖（10 场景，9 完成 + 1 可选）

| # | 场景 | 状态 | 测试 |
|---|------|------|------|
| 1 | 并发冲突（两进程抢同一设备） | ✅ | `e2e_concurrency::test_concurrent_lease_acquisition_conflict` |
| 2 | Lease 正常生命周期 | ✅ | `e2e_integration`（Day 1）+ `execute_action` 内 Lease 集成覆盖 |
| 3 | 进程崩溃恢复（孤立 Lease） | ✅ | `e2e_integration::test_orphaned_lease_recovery_integration` |
| 4 | Lease 过期清理（进程存活） | ✅ | `e2e_integration::test_expired_lease_cleanup_integration` |
| 5 | 多 Task 并发（不同设备） | ✅ | `e2e_concurrency::test_multi_task_concurrent_execution_on_different_devices` |
| 6 | 后台清理线程稳定性 + CPU | ✅ | `e2e_concurrency`（stability + exceptions）+ `e2e_performance::test_background_cleanup_cpu_idle` |
| 7 | Checkpoint 去重（重复扫描） | ✅ | `e2e_integration::test_checkpoint_deduplication_on_repeated_cleanup` |
| 8 | 跨平台进程检测 | ✅ | `test_device_lease_manager.py` 单元 + Day 1/2 集成实测（存活/死亡 PID 双路径） |
| 9 | execute_action 完整流程 | ✅ | `e2e_integration`（SUCCESS 提交 + FAIL 恢复）+ `execute_action` 4 个 Day 3 增强测试 |
| 10 | 真实设备测试 | 🎯 可选 | 需真实 Android 设备 + AdbActionExecutor，低优先级 |

---

## 4. 测试清单

Week 7 相关测试共 **20 个**，分布在 4 个文件（其中 `execute_action` 文件的 8 个里 4 个为 Week 4 已有、4 个为 Day 3 新增；Week 7 净新增 **16 个**）：

| 文件 | 测试数 | 说明 |
|------|--------|------|
| `test_runtime_kernel_e2e_integration.py` | 5 | 孤立恢复、过期清理、Checkpoint 去重（Day 1）；execute 完整流程 SUCCESS/FAIL（Day 3） |
| `test_runtime_kernel_e2e_concurrency.py` | 4 | 多 Task 并发、acquire 冲突、清理线程稳定性、异常容错（Day 2） |
| `test_runtime_kernel_execute_action.py` | 8 | Week 4 基础 4 + Day 3 增强 4（INPUT_TEXT/HOME 分发、LeaseConflict、防重放、防过期决策） |
| `test_runtime_kernel_e2e_performance.py` | 3 | Day 4 基准：单操作开销、50 设备规模、清理线程空闲 CPU |

---

## 5. 性能基准结果（Day 4）

| 基准 | n | 中位数 | p99 | max | 判定 |
|------|---|--------|-----|-----|------|
| `acquire_lease` | 200 | 7.9ms | 8.9ms | 10.2ms | ✅ 远低于 50/500ms 上限 |
| `renew_lease` | 600 | 7.9ms | 8.3ms | 10.1ms | ✅ |
| `release_lease` | 200 | 7.9ms | 10.2ms | 19.2ms | ✅ |
| 50 设备并发获取+释放 | 50+50 | 6.4ms | — | 总计 690ms | ✅ |
| 后台清理线程 CPU（空闲） | 10s 窗口 | **0.16%** | — | — | ✅ 达到 <1% 目标 |

**解读**:
- 单操作 ~8ms 主要来自每次调用新建 SQLite 连接（`sqlite3.connect` + `PRAGMA foreign_keys = ON`，无连接池、非 WAL）。对 Lease 的实际操作频率（每 Task 每分钟几次）完全可接受。若未来频率上升，优化方向：连接复用/池化或启用 WAL。
- 后台清理线程 1s 轮询、空闲 0.16% CPU，确认无 busy-loop（断言上限 5% 为防 busy-loop 闸门，<1% 为目标值）。
- 基准断言均采用宽松上限，防止 CI/开发机抖动导致误报；精确数值由测试 `print` 到 stdout 供本报告引用。

---

## 6. 关键发现

1. **Lease 冲突原子性**: `BEGIN IMMEDIATE` + `UNIQUE(device_id)` 保证线程并发下恰好一个 winner，失败方收到 `LeaseConflict` 且成功方 Lease 不受影响。
2. **恢复幂等**: 同一孤立 Lease 被清理线程重复扫描时，Checkpoint 只创建一次（第二次扫描检测到已存在并跳过），恢复流程可安全重放。
3. **过期 vs 孤立双路径**: 清理线程对每个过期 Lease 先做进程存活检测——存活走"过期警告"路径（记录日志），死亡走"孤立恢复"路径（创建带 `unresolved_action_ref` 的 Checkpoint）。两条路径均有集成实测。
4. **execute_action 防护链**: 非 PROPOSED Action 重放被拒（"only a PROPOSED Action can be dispatched"）、基于过期观察的决策被拒（"Action decision is stale"）、设备被占用时 `LeaseConflict` 透传——三层防护均有测试。
5. **FAIL 裁决不阻断恢复**: verify FAIL 时 Action 标记 FAILED、Stage 保持 ACTIVE、Task 保持 RUNNING、不创建 Checkpoint，后续可基于新观察直接 propose。

### 资源泄漏检查（成功标准项）

- 每个测试结尾显式 `kernel.close()`（无 lease_manager）或 `kernel.shutdown()`（有 lease_manager，含停止清理线程）
- SQLite 连接每次调用即开即关（`with self._connection()`），无长生命周期连接泄漏
- 清理线程 `stop_background_cleanup()` join 超时 5s，测试中确认线程终止（`_cleanup_running is False`）
- 测试库均为 `tmp_path` 临时文件，测试间零残留；未观察到内存/资源泄漏迹象

---

## 7. 剩余风险

| 风险 | 等级 | 说明 |
|------|------|------|
| 真实设备路径未集成验证 | 低 | `AdbActionExecutor` 仅有单元级覆盖；Fake 已验证全部 Kernel 逻辑。场景 10 可选，建议 Phase 6 前用真机跑一次冒烟 |
| Windows 进程检测边界 | 极低 | `PermissionError` 路径仅 Unix 相关，Windows 下走 PID 存在性检查（已实测存活/死亡双路径） |
| Week 5 Deadline 保护未实施 | 中（计划内） | 长时间执行超时的强制回收尚未实现，计划保留 |
| Week 6 管理 UI/API 未实施 | 中（计划内） | Lease 管理视图尚未提供，当前只能通过 DB/日志检查 |

---

## 8. 故障排查指南（Week 7: Lease 与 execute_action）

> 本仓库此前无独立故障排查文档，本节为首版，覆盖 Week 7 涉及的常见故障。

### 8.1 症状 → 原因 → 排查 → 处置

| 症状 | 可能原因 | 排查 | 处置 |
|------|----------|------|------|
| `acquire_lease` 抛 `LeaseConflict` | 设备已被其他 Task/进程持有 | 查 `SELECT * FROM runtime_device_leases WHERE device_id = ?`，看 `holder_process_id` / `task_id` | 等待对方 `release` 或 TTL 过期；确认持有进程是否应存活（存活→可能漏 release；死亡→等清理线程回收） |
| `renew_lease`/`release_lease` 抛 `LeaseNotFound` | lease_id 错误，或 Lease 已被释放/回收 | 用 `get_lease_for_device(device_id)` 查当前设备 Lease 的 id | 重新 acquire；注意 Lease 被清理回收后旧 id 永久失效，不可续期 |
| 孤立 Lease 未被回收 | 清理线程未启动 | 检查 `DeviceLeaseManager._cleanup_running`；`start_background_cleanup` 重复调用会警告并 no-op（不重启） | 启动一次 `start_background_cleanup(interval_seconds=N)`；确认 N 为整数秒（分段 sleep 实现） |
| 过期 Lease 存在但进程存活，日志报"expired but process … is still alive" | 持有进程未 renew（卡顿/死循环） | 看日志警告时间线；检查持有进程状态 | 这是**设计内行为**：Lease 过期后设备可被他人获取，同时保留警告供人工判断持有方健康度 |
| Checkpoint 带 `unresolved_action_ref` 阻塞 propose | 崩溃发生在 EXECUTED 未 verify 时，恢复创建 Checkpoint | 查 `list_checkpoints`，看 `resume_reason` 与 `unresolved_action_ref` | 人工/上游 verify 该 Action 后 Checkpoint 解除；SUCCESS 提交后 ref 清空 |
| "only a PROPOSED Action can be dispatched" | 对已 EXECUTED/FAILED/COMMITTED Action 重复调用 `execute_action` | 查 Action 状态 | 属防重放保护，正常；需要重试时应 propose 新 Action |
| "Action decision is stale" | 决策所基于的 Observation 已不是最新 | 对比 Action 决策时观察与 `list_observations` 最新项 | 基于最新 Observation 重新 propose |
| 日志 "Lease cleanup failed" | 清理循环内部异常（DB 锁、磁盘错误等） | 看异常 stack；清理循环会捕获并继续下一轮 | 通常是瞬时 SQLite 锁；持续出现则检查磁盘/连接数 |
| SQLite "database is locked" | 并发写冲突（非 WAL 模式，rollback journal） | 检查是否有未关闭连接或多个进程长事务 | 当前每操作即开即关，窗口极短；若出现，先排查是否有代码持有连接不放 |
| 清理线程 CPU 异常高 | busy-loop 或 Lease 表异常膨胀 | 跑 `test_background_cleanup_cpu_idle` 复现（正常 <1%）；`SELECT COUNT(*) FROM runtime_device_leases` | 正常值 0.16%；若异常，检查是否存在大量过期 Lease 每轮全量扫描 |

### 8.2 诊断速查

```sql
-- 当前所有 Lease
SELECT id, device_id, task_id, holder_process_id, acquired_at, expires_at, action_id
FROM runtime_device_leases;

-- 已过期但未被回收的 Lease
SELECT * FROM runtime_device_leases WHERE expires_at <= <now-iso>;

-- 某设备的 Lease 历史（需结合 runtime_events 时间线）
SELECT * FROM runtime_device_leases WHERE device_id = ?;
```

- 进程存活检测：`DeviceLeaseManager._is_process_alive(pid)`（Windows 用 `os.kill(pid, 0)` 等价检查）
- 清理线程状态：`_cleanup_running`（bool）/ `_cleanup_thread.is_alive()`
- 停止顺序：`kernel.shutdown()` = 停清理线程 + 关 store；仅 `kernel.close()` 不会停清理线程

---

## 9. 代码审查（Day 5）

审查范围：Week 7 的 4 个测试文件 + Day 4 新增的 `device_lease_manager` 用法。

- ✅ 导入干净：4 个文件无未使用导入（逐文件核对）
- ✅ Fake 一致性：`FullFakeExecutor` 实现 `ActionExecutorPort` 全部 5 个方法并记录调用，Day 3/Day 4 测试共用同一风格
- ✅ 生命周期纪律：无 lease_manager 的 kernel 一律 `close()` 收尾；有 lease_manager 的一律 `shutdown()`；清理线程测试断言终止
- ✅ 隔离性：所有 SQLite 测试使用 `tmp_path`，无跨测试状态
- ✅ 断言策略：功能测试精确断言（状态/字段/日志），基准测试宽松上限 + stdout 报告
- ✅ 无需重构：未发现死代码、重复逻辑或风格不一致

---

## 10. 结论

Week 7 端到端集成测试**完成**：10 个场景中 9 个已验证（1 个可选真机场景留待后续），20 个相关测试全部通过，全量回归 510 passed。Lease 生命周期、并发冲突、崩溃恢复、清理线程、execute_action 防护链均达到计划验证点；性能基准全部达标（单操作 ~8ms、50 设备 690ms、空闲 CPU 0.16% < 1% 目标）。

**Phase 5 状态更新**: Week 1-4 + Week 7 完成；Week 5（Deadline 保护）与 Week 6（管理 UI/API）计划保留未实施。  
**下一步建议**: 开始 Phase 6（Gateway 契约），或按需补做 Week 5/6；真机冒烟（场景 10）建议在 Phase 6 集成前执行一次。

---

**报告日期**: 2026-08-19  
**提交记录**: Day 1 `b1f1810` / Day 2 `6a508cd` / Day 3 `54d5ce0` / Day 4 `357eccc` / Day 5（本报告）
