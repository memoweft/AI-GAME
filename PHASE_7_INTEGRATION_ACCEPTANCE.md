# Phase 7 集成阶段：Legacy 切流验收 + 全量回归

> **历史 Foundation 验收证据，不是当前产品路线图。** 当前权威规范见 [`docs/product/00_INDEX.md`](docs/product/00_INDEX.md)。

**日期**: 2026-08-20
**计划依据**: `PHASE_7_LEGACY_CUTOVER_PLAN.md`（§2 设计、§3 分周、§6 验收标准）
**回滚依据**: `PHASE_7_ROLLBACK_RUNBOOK.md`
**前置**: Week 1（guard + 接线 + 门控 + Deprecation）、Week 2（快照 + 排空门禁）、Week 3（监控端点 + 回滚）完成

---

## 0. 结论

计划 §6 的 **7 项验收标准全部通过**（7/7），全部证据来自自动化测试。
全量回归：后端 **708 passed**（基线 688 + 新增 20）、前端 **52 passed**、前端 `tsc --noEmit && vite build` 绿。

切流为**非破坏性**：模式只改「Legacy 写路径是否开放」，不删数据；回滚 = 配置回退 +（按需）快照恢复。
真机冒烟：模式切换不依赖 adb，全量离线验证；延续 Phase 6「待设备」不构成本阶段阻塞。

---

## 1. §6 七项逐项核对

| # | 验收标准 | 结论 | 证据（测试） |
|---|---|---|---|
| 1 | 三种模式下 §2.1 端点×模式矩阵全部符合 | ✅ | `test_legacy_cutover.py`：`test_legacy_mode_all_write_endpoints_available`（全 202）、`test_draining_mode_blocks_new_tasks_allows_inflight`（创建 403 / 存量输入+停止 202）、`test_kernel_active_mode_blocks_all_legacy_writes`（三写 403 / 只读 200） |
| 2 | `LEGACY_TASK_WRITE_DISABLED`(403) 在 DRAINING（创建）与 KERNEL_ACTIVE（创建/输入/停止）正确启用 | ✅ | 同上三个矩阵用例：断言 403 且 `body["error"]["code"] == "LEGACY_TASK_WRITE_DISABLED"`（2 键 `{code,message}`） |
| 3 | `GET /events` 携带 `Deprecation: true` | ✅ | `test_legacy_cutover.py::test_events_deprecation_header_in_all_modes`（三模式参数化，均 200 + 头） |
| 4 | 切流前快照可生成并可恢复 | ✅ | `test_legacy_cutover_helpers.py`：`test_snapshot_produces_consistent_copy`（在线备份、行数/状态一致、文件名规整）、`test_snapshot_missing_source_creates_placeholder`（缺失→合法占位）、`test_snapshot_restore_returns_db_to_prestore_state`（坏写→覆盖恢复→与切流前逐行一致） |
| 5 | `GET /runtime/mode` 返回当前模式与在途存量 | ✅ | `test_legacy_cutover_integration.py::test_runtime_mode_endpoint_booleans`（三模式 mode+布尔）+ `test_drain_gate_progression_via_mode_endpoint`（在途计数 2→0、`active_task_ids`、`drain_gate_satisfied` 推进） |
| 6 | KERNEL_ACTIVE → LEGACY 回滚后 Legacy 写恢复 | ✅ | `test_legacy_cutover_integration.py::test_cutover_and_rollback_sequence`（legacy→draining→kernel_active→legacy 全程：`POST /tasks` 202→403→403→**202**） |
| 7 | 全量回归绿 + 本验收文档 + roadmap 更新 | ✅ | 见 §2 全量回归；本文档 + `RUNTIME_KERNEL_ROADMAP_STATUS.md` Phase 7 更新 |

### 1.1 守卫单测（§2.6 行为基础）

`test_runtime_mode.py`（9 测试）覆盖 `RuntimeModeGuard`：
- env 解析 + `validate_runtime_mode`（三合法值接受、非法值拒绝 → 启动 fail-fast）；
- 三模式下 `require_legacy_writable` / `require_kernel_active` 的通过/拒绝；
- `require_legacy_runtime_available`（**仅 KERNEL_ACTIVE 拒绝**，DRAINING 放行存量输入/停止）；
- 所有 `RuntimeModeError` 消息**可操作**（指明当前模式 + 应如何处置）。

### 1.2 边界与非破坏性（计划 §5）

- **不混合挂载**：本阶段未触碰 Gateway 挂载；`create_app(gateway=...)` 默认 OFF 保持（Phase 6 回归不变）。
- **只读归档**：任意模式下 `GET /tasks`、`GET /tasks/{id}` 恒 200（矩阵用例断言），数据保留为只读归档。
- **排空门禁非硬阻塞**：`drain_gate_satisfied(active_count)` 仅在 `count == 0` 时为 True；推进 KERNEL_ACTIVE 前由运维据 `GET /runtime/mode` 的 `active_legacy_task_count`/`active_task_ids` 人工确认（runbook §1）。
- **`/inputs` DRAINING 放行**为设计决策（便于存量自然排空）；如需「排空期一律禁输入」，单端点改 `require_legacy_writable`（计划 §5 已标注）。

---

## 2. 全量回归

| 套件 | 结果 | 说明 |
|---|---|---|
| 后端 pytest | **708 passed** | 基线 688 + 新增 20（`test_runtime_mode.py` +2 + 三个新 cutover 文件共 18：cutover 7 + cutover_helpers 6 + cutover_integration 5） |
| 前端 vitest | **52 passed** | 与 Phase 6 集成一致（本阶段无前端改动） |
| 前端 build | ✅ 绿 | `tsc --noEmit && vite build` |

命令：

```powershell
# 后端（F:\AI-GAME）
F:\AI-GAME\apps\console\backend\.venv\Scripts\python.exe -m pytest apps\console\tests\backend -q
# 前端（F:\AI-GAME\apps\console\frontend）
npm run test
npm run build
```

---

## 3. 交付物

**后端**
- `ai_game_console/runtime_mode.py`：`validate_runtime_mode` + `RuntimeModeGuard`（`.mode`、`require_legacy_writable`、`require_legacy_runtime_available`、`require_kernel_active`、`is_*` 查询、可操作错误消息）。
- `ai_game_console/api.py`：`create_app` 接线（`validate_runtime_mode` → `RuntimeModeGuard` → `app.state.runtime_mode_guard`）；`_reject_legacy_write`（被拒写 warning 日志：端点 + 模式 + 原因）+ `enforce_legacy_writable` / `enforce_legacy_runtime_available`；门控 `POST /tasks`、`/inputs`、`/stop`；新增 `GET /runtime/mode`（`RuntimeModeResponse`）；`GET /events` 加 `Deprecation: true`。
- `ai_game_console/schemas.py`：`RuntimeModeResponse`。
- `ai_game_console/mobile_agent/store.py` / `archive.py` / `runtime.py`：`active_tasks()`（在途存量查询，`queued/planning/running/stopping`）。
- `ai_game_console/legacy_cutover.py`：`snapshot_legacy_mobile_tasks`（SQLite 在线备份 + 缺失占位）、`drain_gate_satisfied`、`ACTIVE_LEGACY_STATUSES`。

**测试（Phase 7 新增 20：既有守卫单测 +2 + 三个新 cutover 文件 18）**
- `tests/backend/test_runtime_mode.py`（现 9；Phase 7 新增 2：`legacy_runtime_available` + 其可操作消息）
- `tests/backend/test_legacy_cutover.py`（7，新增）
- `tests/backend/test_legacy_cutover_helpers.py`（6，新增）
- `tests/backend/test_legacy_cutover_integration.py`（5，新增）

**文档**
- `PHASE_7_LEGACY_CUTOVER_PLAN.md`（计划，§6 七项验收）
- `PHASE_7_ROLLBACK_RUNBOOK.md`（配置回退 + 快照恢复 + 验证清单）
- `PHASE_7_INTEGRATION_ACCEPTANCE.md`（本文档，§6 7/7 证据表）
- `RUNTIME_KERNEL_ROADMAP_STATUS.md`（Phase 7 标记 ✅ + 测试总数更新）

---

## 4. 回滚语义（摘要，详见 runbook）

- **配置回退**：`AI_GAME_RUNTIME_MODE=legacy`（或 `draining`）+ 重启 → Legacy 写恢复，数据原样保留。
- **数据回滚**：用切流前快照 `mobile-tasks-<ts>.db` 覆盖 `mobile-tasks.db`（文件级），行数/状态回到切流前（`test_snapshot_restore_returns_db_to_prestore_state` 证明）。
- **监控**：`GET /runtime/mode`（mode + `legacy_writable`/`kernel_active`/`draining` + 在途存量）；每次被拒 Legacy 写打 `warning` 日志（端点 + 模式 + 原因）。
