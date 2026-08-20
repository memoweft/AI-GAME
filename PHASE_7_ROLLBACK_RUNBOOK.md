# Phase 7 回滚 Runbook（Legacy 切流）

> 适用：`AI_GAME_RUNTIME_MODE` 驱动的三阶段切流（LEGACY → DRAINING → KERNEL_ACTIVE）。
> 原则：**模式切换非破坏性**（不删数据），回滚 = 配置回退 + （如需）快照恢复 + 验证。
> 配套文档：`PHASE_7_LEGACY_CUTOVER_PLAN.md`（设计与验收）。

---

## 0. 关键事实速查

| 项 | 值 |
|---|---|
| 模式环境变量 | `AI_GAME_RUNTIME_MODE`（`legacy` / `draining`（或 `drain`） / `kernel_active`（或 `kernel`、`new`）） |
| 默认模式 | `legacy`（未设置 / 非法值一律回落 legacy，见 `config.py::_parse_runtime_mode`） |
| Legacy 任务库 | `<data_dir>/mobile-tasks.db`（`data_dir` 默认 `runtime/console`） |
| 快照目录 | `<data_dir>/backups`（即 `runtime/console/backups`） |
| 快照文件名 | `mobile-tasks-<YYYYmmddTHHMMSSffffff>.db` |
| 快照函数 | `ai_game_console.legacy_cutover.snapshot_legacy_mobile_tasks(source_path, backup_dir)` |
| 监控端点 | `GET /api/v1/runtime/mode` |
| 被禁写码 | `LEGACY_TASK_WRITE_DISABLED`（HTTP 403，2 键 `{"error": {code, message}}`） |
| 受门控的 Legacy 写 | `POST /tasks`、`POST /tasks/{id}/inputs`、`POST /tasks/{id}/stop` |

**模式语义**（`GET /runtime/mode` 布尔与端点行为）：

| 端点 | LEGACY | DRAINING | KERNEL_ACTIVE |
|---|---|---|---|
| `POST /tasks` | 202 | **403** | **403** |
| `POST /tasks/{id}/inputs` | 202 | 202 | **403** |
| `POST /tasks/{id}/stop` | 202 | 202 | **403** |
| `GET /tasks`、`GET /tasks/{id}` | 200（只读归档） | 200 | 200 |
| `GET /events` | 200 + `Deprecation` | 200 + `Deprecation` | 200 + `Deprecation` |

---

## 1. 切流前（进入 KERNEL_ACTIVE 之前）—— 必做

1. **确认排空门禁**：
   ```
   GET /api/v1/runtime/mode
   ```
   确认 `active_legacy_task_count == 0`（`drain_gate_satisfied` 通过）。
   若 > 0：`active_task_ids` 即仍未排空的存量任务；对其执行 `POST /tasks/{id}/stop`（DRAINING 期仍放行），或等待其自然完成，直至计数归零。
   > 设计说明：门禁**不硬阻塞**，允许运维在确认取消后推进；但推进前必须人工确认。

2. **生成切流前快照**（供回滚与审计）：
   ```python
   from pathlib import Path
   from ai_game_console.legacy_cutover import snapshot_legacy_mobile_tasks

   data_dir = Path("runtime/console")
   snap = snapshot_legacy_mobile_tasks(data_dir / "mobile-tasks.db", data_dir / "backups")
   print(snap)  # runtime/console/backups/mobile-tasks-<ts>.db
   ```
   - 源库存在：SQLite `backup` API 在线一致性备份（无需停写）。
   - 源库缺失：生成合法空占位库（回滚后为空库，符合预期）。
   - **记录快照路径**，回滚时需要它。

3. **设置模式并重启**：
   ```
   AI_GAME_RUNTIME_MODE=kernel_active
   ```
   重启后端。启动期 `validate_runtime_mode` 会打印模式日志（fail-fast：非法值拒绝启动）。

4. **验证 KERNEL_ACTIVE 生效**：
   ```
   GET /api/v1/runtime/mode
   # 期望: {"mode": "kernel_active", "legacy_writable": false, "kernel_active": true, "draining": false, ...}

   POST /api/v1/tasks  → 403 LEGACY_TASK_WRITE_DISABLED
   GET  /api/v1/tasks  → 200（只读归档仍可用）
   ```

---

## 2. 回滚决策点

在 KERNEL_ACTIVE（或 DRAINING）出现以下任一情况时，考虑回滚：
- Legacy 读归档异常（`GET /tasks` 报错）；
- 需要重新接纳 Legacy 写（新任务回流）；
- Kernel 侧接管设备/任务出现不可接受的回归。

> 回滚**不丢数据**：Legacy 数据始终留在 `mobile-tasks.db`，模式只改「写路径是否开放」，不改数据。

---

## 3. 回滚操作（配置回退，首选）

1. **改回模式**：
   ```
   AI_GAME_RUNTIME_MODE=legacy        # 完全恢复 Legacy 写
   # 或 AI_GAME_RUNTIME_MODE=draining  # 只放行存量输入/停止，仍拒绝新建
   ```
2. **重启后端**。
3. **验证恢复**：
   ```
   GET /api/v1/runtime/mode
   # legacy:   {"mode": "legacy", "legacy_writable": true, "kernel_active": false, ...}
   # draining: {"mode": "draining", "legacy_writable": false, "draining": true, ...}

   POST /api/v1/tasks  → 202（legacy）或 403（draining，符合预期）
   GET  /api/v1/tasks  → 200
   ```

---

## 4. 数据回滚（快照恢复，按需）

仅当切流后 `mobile-tasks.db` 发生**坏写 / 数据漂移**需要恢复到切流前状态时使用。

> ⚠️ 文件级覆盖：恢复会丢弃快照之后对 `mobile-tasks.db` 的所有写入。执行前确认无在途写（最好先停后端）。

1. **停后端**（释放库文件句柄；Windows 下打开的 SQLite 连接会锁文件，必须无进程持有）。
2. **用快照覆盖原库**：
   ```python
   from pathlib import Path

   data_dir = Path("runtime/console")
   source = data_dir / "mobile-tasks.db"
   snapshot = data_dir / "backups" / "<第 1.2 步记录的快照文件名>"

   # 建议先留一份「当前坏库」副本用于排查：
   source.copy2(data_dir / "backups" / "mobile-tasks-pre-restore.db")
   snapshot.replace(source)
   ```
3. **启动后端**，验证：
   ```
   GET /api/v1/tasks  → 200，行数/状态与切流前快照一致
   GET /api/v1/runtime/mode  → active_legacy_task_count 回到切流前在途数
   ```

> 测试证明：`tests/backend/test_legacy_cutover_helpers.py::test_snapshot_restore_returns_db_to_prestore_state`
> （快照 → 模拟坏写 → 覆盖恢复 → 行数/状态与切流前完全一致）。

---

## 5. 验证清单（每次回滚后逐项打勾）

- [ ] `GET /runtime/mode` 的 `mode` 与预期一致，`legacy_writable` / `kernel_active` / `draining` 布尔正确。
- [ ] `POST /tasks` 行为符合目标模式（legacy 202 / draining|kernel_active 403 `LEGACY_TASK_WRITE_DISABLED`）。
- [ ] `GET /tasks`、`GET /tasks/{id}` 返回 200 且数据完整（只读归档可用）。
- [ ] （数据回滚时）任务行数/状态与切流前快照一致。
- [ ] 后端启动日志中 `validate_runtime_mode` 打印的目标模式与配置一致。

---

## 6. 自动化覆盖（本 runbook 的行为已被测试证明）

| 步骤 | 测试 |
|---|---|
| 模式×端点矩阵（三模式） | `tests/backend/test_legacy_cutover.py` |
| `/runtime/mode` 布尔 + 排空推进 + 切流/回滚序列 | `tests/backend/test_legacy_cutover_integration.py` |
| 快照一致性 / 缺失占位 / **快照恢复** | `tests/backend/test_legacy_cutover_helpers.py` |
| 守卫三模式行为 + 消息可操作 | `tests/backend/test_runtime_mode.py` |
