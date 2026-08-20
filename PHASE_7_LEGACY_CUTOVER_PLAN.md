# PHASE_7 实施计划（Legacy 迁移）

> 目标：三阶段平滑切换，零停机迁移（对齐 `RUNTIME_KERNEL_ROADMAP_STATUS.md` Phase 7）。
> 前置：Phase 6 已完成（Gateway 契约 + SSE + 客户端投影 + 控制 UI，§17 全清单 10/10，2026-08-20）。
> 本文档为 Phase 7 的实施计划与验收基线；每步配自动化测试并保持全量回归绿。

---

## 1. 背景与现状盘点

Phase 7 要把「设备/任务的所有权」从 Legacy（`MobileTaskRuntime`，`mobile-tasks.db`）平滑切换到 Kernel（`runtime_kernel`，`runtime.db`），做到零停机。关键事实（已逐一核实现网代码）：

### 1.1 已存在的运行时模式基础设施（未接入）
- `config.py`
  - `RuntimeMode = Literal["legacy", "draining", "kernel_active"]`（第 9 行）。
  - `Settings.runtime_mode: RuntimeMode = "legacy"`（第 38 行，注释：controls legacy vs new kernel device ownership）。
  - `from_env` 读取环境变量 `AI_GAME_RUNTIME_MODE`（第 167-169 行）。
  - `_parse_runtime_mode`：`draining|drain → draining`；`kernel_active|kernel|new → kernel_active`；其余/默认 → `legacy`（第 227-234 行）。
- `runtime_mode.py`
  - `RuntimeModeError`（`RuntimeError` 子类，第 19 行）。
  - `validate_runtime_mode(mode)`：启动期一致性校验 + 日志（第 22-40 行）。
  - `RuntimeModeGuard`：`require_legacy_writable()`（DRAINING/KERNEL_ACTIVE 拒绝）、`require_kernel_active()`（LEGACY/DRAINING 拒绝）、`is_legacy_writable()`、`is_kernel_active()`、`is_draining()`（第 43-89 行）。
- `tests/backend/test_runtime_mode.py`：已覆盖 env 解析、校验、三种模式 guard 行为、错误消息可操作（共 5 个用例）。

> 结论：三阶段模式的「配置 + 守卫 + 校验」骨架已存在，但**尚未接入任何 HTTP 端点或应用工厂**（全后端仅 `runtime_mode.py` 定义 + 测试引用）。Phase 7 的核心工作是**接入并补齐**，而非从零设计。

### 1.2 Legacy 端点表面（`api.py`，均定义于 `create_app` 闭包内）
| 端点 | 行号 | 行为 | 处置 |
|---|---|---|---|
| `POST /tasks`（`create_mobile_task`） | 1348-1362 | `runtime.start(...)` 202 | 按模式门控（DRAINING/KERNEL_ACTIVE 拒绝） |
| `GET /tasks`（`list_mobile_tasks`） | 1364-1370 | `resolved_mobile_task_archive.list` 200 | 保留（只读归档） |
| `GET /tasks/{task_id}`（`inspect_mobile_task`） | 1372-1374 | `resolved_mobile_task_archive.inspect` 200 | 保留（只读归档） |
| `POST /tasks/{task_id}/inputs` | 1376-1388 | `runtime.send(...)` 202 | 按模式门控（KERNEL_ACTIVE 拒绝） |
| `POST /tasks/{task_id}/stop` | 1390-1398 | `runtime.stop(...)` 202 | 按模式门控（KERNEL_ACTIVE 拒绝） |
| `GET /events`（`list_events`） | 1117-1123 | `service.list_events` 200 分页 JSON | 软弃用（加 `Deprecation` 头） |
| `require_mobile_task_runtime()` | 1339-1346 | 未配置时 503 `mobile_task_runtime_not_configured` | 保留 |

### 1.3 Legacy 数据模型（`mobile_agent/store.py`，`_SCHEMA_VERSION = 2`，库文件 `data_dir/mobile-tasks.db`）
- 主表 `mobile_tasks`：`task_id`(PK)、`goal`、`target_id`、`skill_id`、`skill_scope_id`、`status`、`input_revision`、`plan_revision`、`active_subgoal_index`、`strategy`、`no_progress_count`、`reflection_count`、`attempt_count`、`cancel_requested`、`verification_satisfied`、`detail`、`error_code`、`skill_memory_version`、`worker_token`、`created_at`、`updated_at`、`finished_at`。
- 关联表：`mobile_task_requests`、`mobile_task_plans`、`mobile_task_inputs`、`mobile_task_attempts`、`mobile_task_reflections`、`mobile_task_events`、`mobile_skill_memories`。
- **状态机**（`status` CHECK）：`queued / planning / running / stopping`（在途，非终态）与 `completed / failed / stopped / uncertain`（终态）。
  - 「在途存量」判定 = `status IN ('queued','planning','running','stopping')`。

### 1.4 应用工厂装配点（`api.py::create_app`，第 537 行起）
- `resolved_settings = settings or Settings.from_env()`（第 556 行）。
- `resolved_mobile_tasks`（runtime）与 `resolved_mobile_task_archive`（第 567、699-703 行附近）；未提供时按 `gui_executor_enabled` + adb + local chat 条件自动组合。
- `app.state.settings = resolved_settings`（第 772 行）。
- `validate_runtime_mode` / `RuntimeModeGuard` 目前**未在此构造或调用**。

### 1.5 错误契约（冻结，见 `PHASE_6_GATEWAY_CONTRACT_PLAN.md` §14）
- `GatewayError.to_dict()` → 4 键 `{code, message, retryable, details}`；`gateway_api.py` 状态映射 `"LEGACY_TASK_WRITE_DISABLED": 403`（第 93 行，`retryable=False`）。
- Legacy 路由使用 `ControlPlaneError(code, message, status_code)` → `as_payload()` 2 键 `{"error": {code, message}}`（`service.py` 21-29 行）。
- **Soul 410 弃用先例**（`api.py` 1415-1423）：`POST /soul/commands`、`/integrations/soul/commands` → `ControlPlaneError(code="legacy_soul_write_disabled", status_code=410)`。Legacy 弃写走 `ControlPlaneError` 是本仓库既有模式。

---

## 2. 设计决策

### 2.1 三阶段模式语义（端点 × 模式）
| 端点 | LEGACY | DRAINING | KERNEL_ACTIVE |
|---|---|---|---|
| `POST /tasks`（创建） | ✓ 202 | ✗ **403** `LEGACY_TASK_WRITE_DISABLED` | ✗ **403** `LEGACY_TASK_WRITE_DISABLED` |
| `POST /tasks/{id}/inputs` | ✓ 202 | ✓ 202 | ✗ **403** `LEGACY_TASK_WRITE_DISABLED` |
| `POST /tasks/{id}/stop` | ✓ 202 | ✓ 202 | ✗ **403** `LEGACY_TASK_WRITE_DISABLED` |
| `GET /tasks`（列表） | ✓ 200 | ✓ 200 | ✓ 200（只读归档） |
| `GET /tasks/{id}`（详情） | ✓ 200 | ✓ 200 | ✓ 200（只读归档） |
| `GET /events`（全局） | ✓ 200 | ✓ 200 + `Deprecation` | ✓ 200 + `Deprecation` |

**判定规则（对齐路线图）**：
- **LEGACY_ACTIVE**：新旧并存，新功能走 Kernel；Legacy 全量可用。
- **DRAINING**：拒绝**新** Legacy Task（`POST /tasks`）；存量任务仍可接收输入/停止（`/inputs`、`/stop`），以便自然排空或主动取消。
- **KERNEL_ACTIVE**：完全禁用 Legacy 写路径（创建/输入/停止均 403）；读路径保留为只读归档。

**守卫映射**：
- `POST /tasks` → 既有 `guard.require_legacy_writable()`（DRAINING/KERNEL_ACTIVE 均拒绝）✓ 直接复用。
- `POST /tasks/{id}/inputs`、`/stop` → **新增** `guard.require_legacy_runtime_available()`（仅 KERNEL_ACTIVE 拒绝；LEGACY/DRAINING 放行）。
  - 理由：`require_legacy_writable` 对 DRAINING 也拒绝，但排空期存量任务必须仍可输入/停止，故需要「仅 KERNEL_ACTIVE 拒绝」的守卫。

### 2.2 错误契约选择（403 + `LEGACY_TASK_WRITE_DISABLED`，2 键 ControlPlaneError）
- Legacy 路由端点在守卫抛 `RuntimeModeError` 时，统一转为
  `ControlPlaneError(code="LEGACY_TASK_WRITE_DISABLED", message=<守卫消息>, status_code=403)`。
- **为何用 2 键 `ControlPlaneError` 而非 4 键 `GatewayError`**：
  - 这些是 **Legacy 路由**端点（非 Gateway 规范端点），本仓库 Legacy 弃写既有模式即 `ControlPlaneError`（Soul 410 先例）。
  - `code` 采用契约冻结的 `LEGACY_TASK_WRITE_DISABLED`、状态码采用契约冻结的 **403**，与 `gateway_api.py` 映射完全一致，保证「启用预留码」语义成立。
- 守卫消息已是可操作英文（如 “Legacy task creation is disabled in DRAINING mode; waiting for existing tasks to complete”），直接作为 `message`，满足既有 `test_runtime_mode.py` 对消息可操作性的断言精神。

### 2.3 Legacy Task 数据迁移（非破坏性：归档 + 快照 + 排空门禁）
- **不做模型转换**：Legacy `mobile_tasks` 与 Kernel `runtime_tasks`（+ stages/observations/actions）数据模型不同，且 DRAINING 语义是「等待存量完成」而非「转换在途任务」；强制转换风险高、违背零停机。
- **保留为只读归档**：Legacy 数据留在 `mobile-tasks.db`，KERNEL_ACTIVE 下 `GET /tasks`、`GET /tasks/{id}`（走 `resolved_mobile_task_archive`）继续可用 → **零数据丢失**。
- **切流前快照**：进入 KERNEL_ACTIVE 前，对 `mobile-tasks.db` 做一次文件级备份（`runtime/console/backups/mobile-tasks-<ts>.db`，用 SQLite `backup` API 保证一致性），供回滚与审计。
- **排空门禁**：提供「在途存量」计数（`status IN (queued,planning,running,stopping)`）。DRAINING → KERNEL_ACTIVE 前若仍有在途任务，监控端点给出告警（不硬阻塞，允许运维在确认取消后推进）。

### 2.4 `GET /events` 软弃用
- 保留 200 行为（避免硬破坏既有客户端），响应加 `Deprecation: true` 头（`Sunset` 可选），并在计划文档标注迁移路径：任务级事件请改用 Gateway 契约的 SSE `POST /tasks/{id}/events/stream`。
- 实现：端点加 `response: Response` 形参并设置头（或返回 `JSONResponse(headers={...})`）。

### 2.5 监控与回滚
- **监控**：新增只读端点 `GET /runtime/mode`，返回
  `{ mode, legacy_writable, kernel_active, draining, active_legacy_task_count, active_task_ids }`
  （`active_*` 由 `resolved_mobile_task_archive` 查询在途存量）。启动期 `validate_runtime_mode` 已输出模式日志；每次被拒的 Legacy 写打 `warning` 日志（含端点 + 模式）。
- **回滚**：模式由配置驱动、切换**非破坏性**（不删数据）。
  - 回滚操作 = 将 `AI_GAME_RUNTIME_MODE` 改回 `legacy`（或 `draining`）并重启 → Legacy 写路径即恢复，数据原样保留。
  - 数据回滚 = 使用切流前快照恢复 `mobile-tasks.db`（如需）。
  - 产出回滚 runbook（见 §5 Week 3）。

### 2.6 应用工厂接线
- `create_app` 内、`resolved_settings` 解析后：
  1. `validate_runtime_mode(resolved_settings.runtime_mode)`（启动期 fail-fast，配置非法即拒绝启动）。
  2. `runtime_mode_guard = RuntimeModeGuard(resolved_settings.runtime_mode)`。
  3. `app.state.runtime_mode_guard = runtime_mode_guard`（供端点/测试/监控读取）。
- Legacy 端点闭包内调用守卫；`RuntimeModeError` → `ControlPlaneError(403)`。

---

## 3. 分周交付与里程碑

### Week 1 — cutover 工单 + 运行时模式接线（核心）
- [ ] `runtime_mode.py`：新增 `require_legacy_runtime_available()`（仅 KERNEL_ACTIVE 拒绝）+ `is_legacy_runtime_available()`；补测试。
- [ ] `api.py::create_app`：`validate_runtime_mode` + 构造 `RuntimeModeGuard` + `app.state.runtime_mode_guard`。
- [ ] 门控 `POST /tasks`（`require_legacy_writable`）、`/inputs`、`/stop`（`require_legacy_runtime_available`），`RuntimeModeError` → `ControlPlaneError(403, LEGACY_TASK_WRITE_DISABLED)`。
- [ ] `GET /events` 加 `Deprecation: true` 头。
- [ ] 测试：三种模式 × 各端点的状态码/错误体矩阵；`/events` 头；守卫单测。
- 里程碑：cutover 工单完成，`LEGACY_TASK_WRITE_DISABLED` 启用，回归绿。

### Week 2 — Legacy Task 数据迁移（快照 + 排空门禁）
- [ ] 切流前快照：SQLite `backup` 一致性备份 `mobile-tasks.db`（提供可调用函数 + 触发点：进入 KERNEL_ACTIVE 的运维动作）。
- [ ] 排空门禁：`resolved_mobile_task_archive` 增加在途存量查询（`active_count` / `active_ids`）。
- [ ] 测试：快照可恢复、在途计数正确（含边界：全终态=0）。
- 里程碑：数据迁移（归档+快照+门禁）完成，回归绿。

### Week 3 — 监控与回滚机制
- [ ] `GET /runtime/mode` 监控端点（§2.5 结构）+ 被拒写日志。
- [ ] 回滚 runbook 文档（配置回退 + 快照恢复 + 验证步骤）。
- [ ] 集成/回滚测试：LEGACY→DRAINING→KERNEL_ACTIVE 全程；KERNEL_ACTIVE→LEGACY 回滚后 Legacy 写恢复；快照恢复校验。
- [ ] 全量回归（后端 pytest + 前端 test/build）保持绿。
- [ ] 产出 `PHASE_7_INTEGRATION_ACCEPTANCE.md`；更新 `RUNTIME_KERNEL_ROADMAP_STATUS.md`。
- 里程碑：Phase 7 完成，验收关闭。

---

## 4. 测试策略
- **单元**：`runtime_mode.py` 守卫新增方法（三模式行为 + 消息可操作）；快照函数；在途计数。
- **集成（HTTP）**：以 `create_app(settings=Settings.from_env({"AI_GAME_RUNTIME_MODE": ...}))` 驱动，断言各端点在三种模式下的状态码与错误体（`code`/`status`/`Deprecation` 头）。
- **数据**：快照→恢复→校验任务行数/状态一致；在途计数在 全在途/全终态/混合 下的正确性。
- **回归**：每步后跑后端全量 `pytest`（基线 688）+ 前端 `npm run test`（基线 52）+ `npm run build`，保持绿。

## 5. 风险与边界
- **不混合挂载**（Phase 6 §3 风险 item 1）：Gateway 保持显式 `create_app(gateway=...)` 默认 OFF；模式切换独立于 Gateway 挂载，二者不耦合。
- **`/events` 弃用推迟到 cutover 工单**（Phase 6 §3 item 3）：本计划 Week 1 落实软弃用。
- **场景 10 真机冒烟**：当前机器无 adb，延续 Phase 6「待设备」；模式切换不依赖真机，可全量离线验证。
- **`/inputs` 在 DRAINING 放行**为设计决策（便于存量自然排空）；若运维倾向「排空期一律禁输入」，仅需将该端点守卫改为 `require_legacy_writable`（一行），已在 §2.1 标注。

## 6. 验收标准（Phase 7 完成定义）
1. 三种模式下 §2.1 端点×模式矩阵全部符合（自动化测试覆盖）。
2. `LEGACY_TASK_WRITE_DISABLED`（403）在 DRAINING（创建）与 KERNEL_ACTIVE（创建/输入/停止）正确启用。
3. `GET /events` 携带 `Deprecation: true`。
4. 切流前快照可生成并可恢复（测试证明）。
5. `GET /runtime/mode` 返回当前模式与在途存量。
6. KERNEL_ACTIVE → LEGACY 回滚后 Legacy 写恢复（测试证明）。
7. 后端全量回归 + 前端 test/build 全绿；`PHASE_7_INTEGRATION_ACCEPTANCE.md` 产出；roadmap 更新。
