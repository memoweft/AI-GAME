# Phase 5 Week 5：Deadline 保护（Lease 绝对截止）

> 状态：✅ 已完成
> 日期：2026-08-19
> 前置：Week 4 设备独占（Lease 基础）已完成；`runtime_device_leases` 表（Schema v4）

## 1. 目标

解决 Week 4 遗留的"无限续期"问题：只要持有进程持续续期（heartbeat），Lease 可以永久存在，
一旦持有进程"活着但卡死"（挂起、死循环、事件循环阻塞），设备将永远无法被其他 Task 使用。

引入 **Deadline（绝对截止）** 机制：

- `expires_at`：可续期 TTL，决定"心跳窗口"，续期可以推迟它；
- `deadline_at`：**绝对截止时刻**，续期**永远无法**推迟；
- 默认 `deadline_at = acquired_at + 5 分钟（300s）`，可在获取时覆盖；
- 超过 Deadline 后：续期请求被拒绝（`LeaseExpired`）；后台清理**即使持有进程仍存活**也强制回收，
  并创建 `resume_reason="deadline_exceeded"` 的恢复 Checkpoint。

## 2. 设计决策

### 2.1 域模型（`runtime_kernel/lease/__init__.py`）

`DeviceExecutionLease` 新增必填字段：

```python
deadline_at: str  # ISO 8601 UTC，绝对截止，续期不可推迟
```

- `__post_init__` 校验 UTC ISO 格式，且 `deadline_at >= expires_at`（配置错误直接 ValueError）；
- `renew()` / `with_action()` 原样携带 `deadline_at`（不可变语义不变）；
- 新增 `is_deadline_exceeded(now)` 辅助判断。

不变式（由域校验 + store 钳制共同保证）：**任意时刻 `expires_at <= deadline_at`**。
因此"超过 Deadline 的 Lease 集合 ⊆ 已过期的 Lease 集合"——清理扫描无需第二条查询路径
（store 仍提供 `list_deadline_exceeded_leases()` 供管理/统计使用）。

### 2.2 存储层（`runtime_adapters/sqlite/store.py`）

**Schema 迁移 v4 → v5**：

```sql
ALTER TABLE runtime_device_leases ADD COLUMN deadline_at TEXT;
CREATE INDEX ix_runtime_device_leases_deadline ON runtime_device_leases(deadline_at);
```

迁移后由 Python 回填存量行：`deadline_at = acquired_at + 300s`（本地开发库，无生产数据）。

**`acquire_lease(..., deadline_seconds: int | None = None)`**：

- 默认 300s；显式传入时若 `deadline_seconds < ttl_seconds` 抛 `ValueError`（截止早于首个 TTL 属配置错误）；
- 插入时写入 `deadline_at`。

**`renew_lease(lease_id, new_expires_at, new_heartbeat_at)`** 新增强制逻辑：

1. `new_heartbeat_at >= deadline_at` → 抛 `LeaseExpired(lease_id, expires_at, now, deadline_at)`；
2. 否则钳制：`effective_expires = min(new_expires_at, deadline_at)`，**续期永不跨越 Deadline**。

**新增查询**：`list_deadline_exceeded_leases(now)`（`deadline_at <= now`，按 deadline 排序）。

### 2.3 错误类型（`runtime_kernel/lease/errors.py`）

`LeaseExpired` 新增可选参数 `deadline_at: str | None = None`（向后兼容）；
因 Deadline 触发时错误信息显式携带 `deadline_at`。

### 2.4 管理器（`device_lease_manager.py`）

`_cleanup_orphaned_leases()` 在既有"过期即释放"路径上增加 Deadline 分支：

- 对每个过期 Lease，若 `is_deadline_exceeded(now)`：
  - 日志明确"超过 Deadline（进程存活与否均强制回收）"；
  - 关联 Action 时创建恢复 Checkpoint，`resume_reason="deadline_exceeded"`
    （复用既有去重逻辑：`latest_checkpoint(task_id).unresolved_action_ref` 相同则跳过）；
- 否则维持原路径：`resume_reason="process_crash_during_lease_hold"`。

`_create_recovery_checkpoint(lease, *, resume_reason=...)` 参数化原因（默认值保持原行为）。

### 2.5 Kernel 调用方（`runtime_kernel/kernel.py`）

`execute_action` 无需改动：`acquire_lease(ttl_seconds=60)` 自动获得默认 300s Deadline，
单 Action 执行窗口远小于 5 分钟。

## 3. 行为矩阵

| 场景 | 行为 |
|---|---|
| 获取时 `deadline_seconds < ttl_seconds` | `ValueError`（配置错误） |
| `now < deadline` 时续期 | 成功；`expires_at = min(请求值, deadline_at)` |
| `now >= deadline` 时续期 | `LeaseExpired`（携带 `deadline_at`） |
| 后台清理：过期且未超 Deadline | 原行为（进程死 → crash 原因 Checkpoint；均释放） |
| 后台清理：超过 Deadline | **无论进程存活与否**强制释放；关联 Action → `deadline_exceeded` Checkpoint |

## 4. 测试（`tests/backend/test_runtime_kernel_lease_deadline.py`）

- 默认 Deadline = acquired_at + 300s；自定义 `deadline_seconds`；
- `deadline < ttl` 拒绝（ValueError）；
- 续期钳制：请求过期点超过 Deadline 时 `expires_at == deadline_at`；
- 超过 Deadline 后续期抛 `LeaseExpired` 且携带 `deadline_at`；
- 清理：超过 Deadline 的 Lease **即使持有进程存活**也被释放；
- 清理：超过 Deadline 且关联 Action 时创建 Checkpoint，`resume_reason == "deadline_exceeded"`；
- Schema 迁移：v4 库（手工构造）→ `initialize()` 后 revision=5，存量行 deadline 回填；
- `list_deadline_exceeded_leases` 返回正确集合。

## 5. 验收标准（对齐 roadmap）

- [x] `DeviceExecutionLease` 新增 `deadline_at` 字段（绝对截止，续期不可推迟）
- [x] `renew_lease()` 检查 `now < deadline_at`，超时抛 `LeaseExpired`
- [x] 默认 Deadline = acquired_at + 5 分钟
- [x] 清理扫描超过 Deadline 的 Lease，即使进程存活也创建 Checkpoint 并释放
- [x] 记录 `deadline_exceeded` 恢复原因
- [x] 测试覆盖：续期被阻止 / 自动清理触发 / Checkpoint 创建

## 6. 关联

- 上游：`PHASE_5_DEVICE_OWNERSHIP_IMPL.md`（Week 4）
- 下游：`PHASE_5_WEEK_6_ADMIN_API.md`（Week 6，Deadline 统计进入管理 API）
