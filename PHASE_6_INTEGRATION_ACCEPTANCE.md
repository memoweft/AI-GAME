# Phase 6 集成阶段: 契约 §17 全清单验收 + 全量回归

**日期**: 2026-08-20
**契约依据**: `docs/NEW/PHASE_1_GATEWAY_CONTRACT_DESIGN.md` §17（前后端联调验收，DESIGN FROZEN）
**计划依据**: `PHASE_6_GATEWAY_CONTRACT_PLAN.md` §2 集成阶段
**前置**: Week 1-4 完成（Week 4 回归 679 passed）

---

## 0. 结论

契约 §17 的 10 项验收标准**全部通过**（10/10），全部证据来自自动化测试。
全量回归：后端 **688 passed**、前端 **52 passed**、前端 `tsc --noEmit && vite build` 绿。

场景 10（真机冒烟）：本机**无 adb 设备**，记录为 **待设备**；该场景的 Fake 证据
（Fake Device Registry + Fake Observation Provider 跑完整 create→message→control→
events→SSE 链路）已由 Week 3/4 与本次集成测试覆盖。

---

## 1. §17 十项逐项核对

契约 §17 原文（"后续 Gateway 实现至少要通过"）：

| # | 契约原文 | 验收结论 | 证据（测试） |
|---|---|---|---|
| 1 | Task create/get | ✅ 通过 | `test_gateway_api.py` `TestCreateTask` / `TestGetTask`（§5/§6 canonical 形状、404/409） |
| 2 | message/control | ✅ 通过 | `test_gateway_api.py` `TestMessages` / `TestControls`（§7/§9 形状、`TASK_NOT_ACTIVE`、未知 command 400） |
| 3 | conversation 唯一关联 | ✅ 通过 | `test_gateway_api.py` `TestConversations`（attach 单活任务 / 无活任务 create / 两活任务 `CONVERSATION_CONFLICT`） |
| 4 | 幂等 create/message/control | ✅ 通过 | `test_gateway_api.py` `TestCreateTask`/`TestMessages`/`TestControls` 的 replay/conflict 用例 + `test_gateway_idempotency.py`（同 key 同 payload 回放、同 key 不同 payload `IDEMPOTENCY_CONFLICT`） |
| 5 | Event sequence 与分页 | ✅ 通过 | `test_gateway_api.py` `TestEvents`（严格递增、`after_sequence` 游标、空页返回游标、limit 分页、非法游标/limit 400） |
| 6 | SSE 断线续传 | ✅ 通过 | `test_gateway_sse.py` `test_stream_resume_after_disconnect_no_duplicates_no_gaps` + `test_stream_reconnect_from_zero_replays_full_log`（真 uvicorn socket） |
| 7 | Snapshot + Event 校准 | ✅ 通过 | `test_gateway_api.py` `TestGetTask::test_snapshot_shape`（`last_event_sequence`）+ `test_gateway_sse.py` 游标重连（用 snapshot 的 `last_event_sequence` 作为 `after_sequence` 续流，无重无漏） |
| 8 | Web/Hermes 共享语义 | ✅ 通过 | `test_gateway_integration.py` `TestSharedSemantics`（同一 task 对 `web-1`/`hermes-1`/`wechat-1` 返回**逐字节一致**的 snapshot 与 event 页） |
| 9 | Client 无法访问 ADB | ✅ 通过 | `test_gateway_integration.py` `TestNoAdbSurface`（Gateway 路由集 == §2 canonical 集、无 adb/shell/screenshot 端点、设备摘要仅 `{id, availability}`、错误信封不携带 ADB 细节） |
| 10 | legacy write 不会绕过 Kernel | ✅ 通过 | `test_gateway_integration.py` `TestLegacyWritesCannotBypassKernel` + `test_gateway_api.py::TestDefaultOff::test_gateway_wins_conflicting_canonical_path`（Gateway 启用后 canonical 任务路径全部由 Gateway 服务并落 Kernel，legacy 重复实现不可达） |

### 1.1 item 8 — Web/Hermes 共享语义

契约 §1「Gateway 不负责让不同 Client 拥有不同任务语义」、§13「微信/Hermes 默认
消费用户可读投影，不转发每次点击和 ADB 日志」。

- `X-Client-Id` 只在 **create**（`gateway_api.py:308`）与 **conversation 入口**
  （`gateway_api.py:472`）被读取并存储；Gateway 的 **get/list/events 等只读路径完全不
  读取该头**，因此不存在按 Client 类型切换语义的分支。
- 证据：`TestSharedSemantics` 对同一 task 分别以 `X-Client-Id: web-1 / hermes-1 /
  wechat-1` 请求 `GET /tasks/{id}` 与 `GET /tasks/{id}/events`，断言三次响应
  **`payloads[0] == payloads[1] == payloads[2]`**（逐字节一致），且共享语义携带
  canonical 投影字段（`status`/`current_stage`/`last_event_sequence`）。

### 1.2 item 9 — Client 无法访问 ADB

契约 §1「Gateway 不负责执行 ADB」、§14「供应商原始错误、ADB stdout/stderr 和内部
traceback 不直接返回普通 Client」。

- Gateway 路由集**恰好**等于 §2 canonical 十路径（`TestNoAdbSurface::
  test_gateway_router_is_exactly_the_canonical_set`），不存在任何 ADB 执行 / 截图 /
  shell 端点（`test_no_adb_execution_endpoint` 对路径做 adb/shell/screenshot/exec/
  input/tap/swipe 黑名单断言）。
- 设备面只暴露 `GET /devices` 的 `{id, availability}` 摘要（Week-3 port-only shape），
  无 ADB serial / 原始输出。
- 错误信封：`DeviceNotFound`/`DeviceNotAvailable`/`InternalError` 的 message 与
  details 均为 client-safe 文案；`test_error_envelope_never_carries_adb_detail` 断言
  响应中不含 `adb/traceback/stderr/serial`。

### 1.3 item 10 — legacy write 不会绕过 Kernel

契约 §2（cutover 模型）、§15（Legacy Adapter 边界：不得直接调用 Kernel Store、
不得保持第二套 Active Task 状态机）。

- `create_app(gateway=...)` 启用后，**gateway router 先于 legacy router 注册**
  （`api.py:1494-1496`）。canonical 任务路径 `POST /tasks`、`GET /tasks`、
  `GET /tasks/{task_id}` 在两个 router 中重名，**先注册的 Gateway 胜出**，因此这些
  写入全部落到 Kernel，legacy `create_mobile_task` 等重复实现**不可达**。
- 证据：`TestLegacyWritesCannotBypassKernel` 在 Gateway 启用后断言
  `POST /tasks` 返回 canonical create 形状（`{id, goal, status, device_id,
  current_stage, last_event_sequence}`）、`GET /tasks` 与 `GET /tasks/{id}` 返回
  canonical snapshot（含 `device_id`/`last_event_sequence`），且**不含**任何 legacy
  `MobileTaskSchema` 专属键（`input_revision`/`active_subgoal_index`/`skill_id`/
  `strategy`/`skill_memory_version`/`target_id`）。
- **边界说明（cutover 工单范围，非本阶段缺陷）**：legacy 专属控制路径
  `POST /tasks/{task_id}/inputs` 与 `POST /tasks/{task_id}/stop` 不在 §2 canonical 集内，
  Gateway 启用后仍可访问 —— 它们写的是旧 `MobileTaskRuntime`（独立旧系统，独立 DB），
  **不触碰 New Runtime Kernel**，故不构成"绕过 Kernel"。按契约 §2，这些 legacy 重复
  端点的移除由独立的 **cutover 工单**负责（见 `PHASE_6_GATEWAY_CONTRACT_PLAN.md`
  风险与边界 §1），本阶段不删、不混挂。

---

## 2. 场景 10 — 真机冒烟

**结论：待设备。** 本机无 adb：

- `adb` 不在 PATH；
- 常见 Android SDK 位置（`%LOCALAPPDATA%\Android\Sdk\platform-tools`、
  `C:\Android\platform-tools`、`D:\Android\platform-tools` 等）均无 `adb.exe`。

因此无法执行真机端到端冒烟。按集成阶段约定记录"待设备"，并保留 **Fake 证据**：
全部 Gateway/Kernel 链路测试均使用 `FakeDeviceRegistry` + `FakeObservationProvider`
+ 文件 Artifact Store，覆盖 create → message → control → events → SSE 的完整链路
（`test_gateway_api.py`、`test_gateway_sse.py`、`test_gateway_integration.py`）。
真机冒烟在具备 adb 设备的环境补齐后，将本条由"待设备"升级为"真机通过"。

---

## 3. 全量回归

| 套件 | 结果 | 说明 |
|---|---|---|
| 后端 pytest | **688 passed**（170s） | 基线 679 + 本次新增 9（`test_gateway_integration.py`） |
| 前端 vitest | **52 passed**（9 files） | 含 `gatewayEventStream`/`gatewayProjection`/App 导航"网关任务" |
| 前端 build | ✅ 绿 | `tsc --noEmit && vite build`（1805 modules） |

命令：

```powershell
# 后端（F:\AI-GAME）
F:\AI-GAME\apps\console\backend\.venv\Scripts\python.exe -m pytest apps\console\tests\backend -q
# 前端（F:\AI-GAME\apps\console\frontend）
npm run test
npm run build
```

---

## 4. 契约冻结与挂载策略保持

- 契约保持 FROZEN：本阶段未新增/修改任何 API，仅新增验收测试。
- `create_app(..., gateway=None)` 默认 **OFF**：默认组合与 legacy app 逐字节一致
  （`TestDefaultOff::test_gateway_routes_absent_on_default_app`、
  `test_default_app_has_no_gateway_state`），legacy 回归保持绿。
- `LEGACY_TASK_WRITE_DISABLED` 错误类已定义（`gateway/errors.py:70`）但当前代码路径
  **未 raise** —— 预留为 cutover 工单在禁用 legacy 写时的返回码。

---

## 5. 交付物

- `apps/console/tests/backend/test_gateway_integration.py`（9 测试，item 8/9/10 证据）
- `PHASE_6_INTEGRATION_ACCEPTANCE.md`（本文档，§17 10/10 证据表）
- `RUNTIME_KERNEL_ROADMAP_STATUS.md`（Phase 6 标记 ✅ + 测试总数更新）
