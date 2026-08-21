# Phase 7 切流演练：真机 Legacy → Draining → Kernel_Active → 回滚

> **历史 Foundation 本机演练证据，不是生产部署记录，也不是当前产品路线图。** 当前权威规范见 [`docs/product/00_INDEX.md`](docs/product/00_INDEX.md)。

**日期**: 2026-08-20
**计划依据**: `PHASE_7_ROLLBACK_RUNBOOK.md`（§0 事实速查、§1 切流前、§2 回滚决策点、§3 配置回退、§4 数据回滚、§5 验证清单）
**前置**: Phase 7 Week 1–3 + 集成（708 passed，`PHASE_7_INTEGRATION_ACCEPTANCE.md`）、Kernel Lease 接线 + 场景 10 真机冒烟（718 passed）
**范围**: 本机真机切流演练；**不改变默认配置**（演练结束 `AI_GAME_RUNTIME_MODE` 未设置，默认 legacy）

---

## 0. 结论

- `legacy → draining → kernel_active → legacy` 四段全循环在真机执行完成，runbook §5 验证清单 **5 项通过**（其中 2 项带已记录的偏差/部分证据，见 §3、§4）。
- **数据完整性**：演练全程零写入；live 归档与切流前快照逐项一致（14 任务，状态分布相同）。
- **回滚成功**：`kernel_active → legacy` 配置回退后写门恢复开放，只读归档全可用；数据回滚（runbook §4）无需执行（无坏写），快照原样保留。
- **默认配置未改变**：演练结束后调用会话与 User/Machine 级 `AI_GAME_RUNTIME_MODE` 均为空，后端以默认 legacy 运行。
- 发现 **2 项低危 finding**（F-1 启动日志可见性、F-2 launcher 日志截断）+ **1 项观察**（F-3 422/403 次序），均非阻塞，建议跟进（§5）。

---

## 1. 演练环境与范围

| 项 | 值 |
|---|---|
| 机器 | Windows 开发机，venv `runtime/envs/console`，后端 `http://127.0.0.1:4310` |
| 设备 | mumu 模拟器（`127.0.0.1:16384`，Android 15）已配置于 `config/executor-runtime.env`；**演练全程未启动真实设备任务**（偏差 D-1） |
| 启动器 | `scripts/console.ps1`（`AI_GAME_RUNTIME_MODE` 经调用会话进程环境继承给后端） |
| 验证器 | `apps/console/scripts/cutover_drill_http_check.py`（live HTTP，幂等可重跑）+ `apps/console/scripts/cutover_drill_db_report.py`（只读 DB 报告） |

**范围内**：legacy 侧写门控、`GET /runtime/mode` 监控、切流前快照、配置回退/回滚路径、数据完整性。
**范围外**：kernel 侧执行链路（kernel 接管设备执行）的真机启用——本次 `kernel_active` 阶段验证的是 legacy 门控关闭 + 监控端点，不是 kernel 跑设备任务。

---

## 2. 执行步骤与证据

### 2.1 基线与排空门禁（runbook §1.1）

- `GET /runtime/mode` → `mode=legacy, legacy_writable=true, kernel_active=false, draining=false, active_legacy_task_count=0, active_task_ids=[]`
- `cutover_drill_db_report.py`（live 归档，只读）：
  ```text
  total_tasks=14
  status_counts={'completed': 7, 'failed': 5, 'uncertain': 2}
  in_flight_count=0
  ```
- 排空门禁满足（在途 0），无需人工确认排空。

### 2.2 切流前快照（runbook §1.2）

- `snapshot_legacy_mobile_tasks(source, backup_dir)` →
  `runtime/console/backups/mobile-tasks-20260820T082224103830.db`（**634,880 字节**，2026-08-20 16:22:24）。

### 2.3 DRAINING 段（runbook §1.3–1.4）

调用会话设置 `AI_GAME_RUNTIME_MODE=draining` 后重启后端。验证（`cutover_drill_http_check.py drain` → **ALL CHECKS PASSED**）：

```text
GET  /runtime/mode → mode=draining, legacy_writable=false, kernel_active=false,
                     draining=true, active_legacy_task_count=0, active_task_ids=[]
POST /tasks（有效载荷 {"goal": "drill: must be rejected", "client_request_id": "drill-1"}）
     → 403 {"error": {"code": "LEGACY_TASK_WRITE_DISABLED",
          "message": "Legacy task creation is disabled in DRAINING mode; waiting for existing tasks to complete"}}
GET  /tasks  → 200（count=14）
GET  /events → 200 + 响应头 Deprecation: true
```

启动日志证据（`console.err.log`，WARNING 级被 Python lastResort 处理器落盘）：

```text
Runtime mode: DRAINING (rejecting new legacy tasks, waiting for drain)
```

### 2.4 KERNEL_ACTIVE 段（runbook §2.1 端点×模式矩阵）

调用会话设置 `AI_GAME_RUNTIME_MODE=kernel_active` 后重启后端。验证（`cutover_drill_http_check.py kernel` → **ALL CHECKS PASSED**）：

```text
GET  /runtime/mode → mode=kernel_active, legacy_writable=false, kernel_active=true, draining=false
POST /tasks → 403 LEGACY_TASK_WRITE_DISABLED
     （"Legacy task creation is permanently disabled in KERNEL_ACTIVE mode"）
POST /tasks/{id}/stop   → 403（"Legacy task control (inputs/stop) is permanently disabled in KERNEL_ACTIVE mode"）
POST /tasks/{id}/inputs → 403（同上）
GET  /tasks/{id} → 200
GET  /tasks  → 200（count=14）
GET  /events → 200 + 响应头 Deprecation: true
```

### 2.5 回滚段（runbook §3 配置回退）

移除 `AI_GAME_RUNTIME_MODE`（默认 legacy）后重启后端。验证（`cutover_drill_http_check.py legacy` → **ALL CHECKS PASSED**；执行两次——回滚后立即验证一次、最终状态再验证一次）：

```text
GET  /runtime/mode → mode=legacy, legacy_writable=true, kernel_active=false, draining=false
GET  /tasks  → 200（count=14）
GET  /events → 200 + 响应头 Deprecation: true
POST /tasks → SKIPPED（偏差 D-1）
```

**数据回滚（runbook §4）：未执行。** 演练全程无写入，无坏写/数据漂移，无需恢复；快照文件保留待命（Windows 下如需恢复：先停后端再覆盖，见 runbook §4 警示）。

---

## 3. §5 验证清单逐项对照

| # | 清单项 | 结论 | 证据 |
|---|---|---|---|
| 1 | `GET /runtime/mode` 的 `mode` 与预期一致，`legacy_writable` / `kernel_active` / `draining` 布尔正确 | ✅ | §2.3/§2.4/§2.5 三模式 JSON（mode + 三布尔 + 在途计数全部符合） |
| 2 | `POST /tasks` 行为符合目标模式（legacy 202 / D、K 403 `LEGACY_TASK_WRITE_DISABLED`） | ✅（⚠️ D-1） | D、K 段实测 403 且 body 含 `code=LEGACY_TASK_WRITE_DISABLED`（§2.3/§2.4）；legacy 段未真实创建（D-1），门控开放由 mode 布尔验证，202 全矩阵由自动化覆盖（§4） |
| 3 | `GET /tasks`、`GET /tasks/{id}` 返回 200 且数据完整 | ✅ | 三模式 `GET /tasks` 200（count=14）；kernel 段 `GET /tasks/{id}` 200（§2.4） |
| 4 | 任务行数/状态与切流前快照一致 | ✅ | live 与快照均为 `total_tasks=14, {'completed':7, 'failed':5, 'uncertain':2}, in_flight=0`（`cutover_drill_db_report.py` 双跑）；快照文件完好（634,880 字节，时间戳未变） |
| 5 | 后端启动日志中 `validate_runtime_mode` 打印的目标模式与配置一致 | ⚠️ 部分（F-1/F-2） | DRAINING 段 WARNING 行落盘且与配置一致（§2.3）；legacy/kernel_active 段 INFO 行未落盘（应用无 logging 配置，Python lastResort 仅 WARNING+），且 launcher 每次启动截断日志（F-1/F-2）——该清单项在现配置下只能部分取证 |

---

## 4. 偏差说明

- **D-1（legacy 段 `POST /tasks` 未真实执行）**：`config/executor-runtime.env` 已启用 GUI executor，legacy 下真实创建会在 mumu 上启动真实设备任务并写入即将冻结的归档库，超出演练意图。替代证据链：
  1. 门控开放：`GET /runtime/mode` 的 `legacy_writable=true` + `active_legacy_task_count=0`（§2.5）；
  2. legacy 三写端点 202 全矩阵：`tests/backend/test_legacy_cutover.py::test_legacy_mode_all_write_endpoints_available`（全 202）；
  3. 切流/回滚序列（含 legacy 202）：`tests/backend/test_legacy_cutover_integration.py::test_cutover_and_rollback_sequence`（`POST /tasks` 202→403→403→**202**）。

---

## 5. 发现（Findings）

| # | 等级 | 发现 | 建议 |
|---|---|---|---|
| F-1 | 低 | `validate_runtime_mode` 的启动模式日志：仅 DRAINING（WARNING 级）落盘；legacy/kernel_active 为 INFO 级，应用无 logging 配置（Python lastResort 处理器只输出 WARNING+）→ §5 清单第 5 项在 legacy/kernel_active 下无法从日志文件取证 | 给应用加 logging 配置（uvicorn log config 或启动时 `logging.basicConfig`），使三模式的启动模式行均可持久化 |
| F-2 | 低 | `scripts/console.ps1` 的 `Start-Process -RedirectStandardOutput/-RedirectStandardError` 以 CREATE_ALWAYS 打开日志文件，每次启动截断 `console.out/err.log` → 上一轮启动的模式证据随重启丢失 | 启动前将旧日志按时间戳改名归档（如 `console-20260820-162224.err.log`）或改用追加模式 |
| F-3 | 观察 | FastAPI 请求体校验（422）先于模式门控（403）执行：D/K 段发送**无效**载荷得到 422 而非 403。runbook §2.1 矩阵默认有效载荷（演练即用有效载荷）。无代码问题（校验先行是合理行为） | runbook §2.1 矩阵处加一行注记：矩阵行为以有效载荷为前提，无效载荷返回 422 |

---

## 6. 剩余风险与下一步建议

1. **kernel 侧执行链路未真机启用**：本次演练覆盖 legacy 门控 + 监控 + 快照/回滚路径；正式切流前还需 kernel 接管设备执行的链路在真机就绪（超出本演练范围，见 roadmap 后续阶段）。
2. **快照保留**：`runtime/console/backups/mobile-tasks-20260820T082224103830.db` 保留待命；如未来发生坏写，按 runbook §4 执行（Windows：先停后端再覆盖）。
3. **F-1 / F-2** 建议立项跟进（日志可见性与留存是回滚演练可审计性的前提）；**F-3** 建议以一行注记并入 runbook。
4. 演练验证脚本已固化为 `apps/console/scripts/cutover_drill_http_check.py` 与 `apps/console/scripts/cutover_drill_db_report.py`，可复用于任何未来复验。

---

## 7. 演练产物

| 产物 | 路径 |
|---|---|
| live HTTP 验证器（三模式，幂等） | `apps/console/scripts/cutover_drill_http_check.py` |
| 归档/快照只读报告 | `apps/console/scripts/cutover_drill_db_report.py` |
| 切流前快照（保留） | `runtime/console/backups/mobile-tasks-20260820T082224103830.db`（634,880 字节） |
| 本报告 | `PHASE_7_CUTOVER_DRILL_REPORT.md` |

## 8. 最终状态

- **后端**：默认 legacy 模式运行（`AI_GAME_RUNTIME_MODE` 未设置）；全量回归 **718 passed**（后端）。
- **环境**：User/Machine 级 `AI_GAME_RUNTIME_MODE` 确认为空；调用会话残留已清理。
- **数据**：归档库 14 任务，状态分布与切流前一致，演练零写入。
