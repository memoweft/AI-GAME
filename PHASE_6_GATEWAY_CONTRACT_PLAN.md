# Phase 6: Gateway 契约实现计划

> 契约依据: `docs/NEW/PHASE_1_GATEWAY_CONTRACT_DESIGN.md` — `DESIGN FROZEN — API NOT MODIFIED`
> 创建: 2026-08-19 | 前置: Phase 5 完成（全量回归 529 passed）

## 0. 决策记录

- **直接开始 Phase 6。** Week 7 真机冒烟（场景 10: ADB tap/swipe/back/home 真机验证）并入
  Phase 6 集成阶段执行 —— 当前机器无 adb，且 E2E 计划已标注场景 10 优先级低、逻辑可用 Fake 验证。
- **Kernel 是唯一状态源。** Gateway 只调用 Kernel 公开应用服务；禁止 Gateway/Legacy 直接
  访问 Store 表或直接获取 Device Lease（契约 §15）。
- **Legacy 路由不动。** `apps/console/backend/ai_game_console/api.py` 中现有 `/api/v1`
  legacy 路由在本 Phase 保持逐字节行为不变；新 Gateway 作为**独立 router 模块**显式挂载。
  共享路径（`POST/GET /tasks`、`GET /tasks/{task_id}` 等）按 payload 猜版本被契约禁止，
  因此不做混合挂载 —— cutover 是单独工单（见 §3 Week 3）。

## 1. 范围

**做:**
1. Kernel 控制面: pause / resume / cancel / takeover + Lease 联动 + 控制事件
2. Gateway 应用服务: 会话唯一关联、Idempotency-Key 幂等、Task Snapshot 投影、消息→Kernel
3. 规范 HTTP API 表面: 契约全部端点 + 10 码错误模型
4. SSE 事件流: `after_sequence` 续传、心跳不占 sequence、慢 Client 隔离
5. 前端: 任务暂停/恢复/取消/接管 UI + 事件增量投影
6. 集成: 场景 10 真机冒烟（有设备时）+ 契约 §17 验收清单 + 全量回归

**不做（契约非职责）:** 任务规划、ADB 执行逻辑、第二套 Active Task 状态机、
legacy 路由改写、部署级安全策略（认证/Artifact 授权仍由部署配置决定）。

## 2. 每周分解

### Week 1 — Kernel 控制面 ✅（2026-08-20，25 tests，回归 554）

`RuntimeKernel.apply_control(task_id, command, reason=None) -> ControlResult`

| 命令 | 合法前置状态 | 目标状态 | 事件 (actor=gateway) |
|---|---|---|---|
| `pause` | RUNNING | PAUSED | `TaskPaused` |
| `resume` | PAUSED | RUNNING | `TaskResumed` |
| `cancel` | 一切非终态 | CANCELLED（terminal_at） | `TaskCancelled` |
| `takeover` | RUNNING | PAUSED | `UserTakeover` |

要点:
- **复用冻结状态机**: 四个转移全部已在 `_ALLOWED_TASK_TRANSITIONS` 中，不改 `task/domain.py`。
- `resume` 只改状态，不重新获取 Lease —— 契约 §9: resume 响应不代表设备动作已恢复，
  Runtime 必须先 Observe；下一次 `execute_action` 按现有流程自行获取 Lease。
- **Lease 联动**: pause/cancel/takeover 若任务持有 active lease（`get_lease_for_task`）则释放；
  `release_lease` 是幂等 DELETE，与 `execute_action` 的 finally 释放无双重释放冲突。
- 新 Store 端口 `mutate_task(before_task, after_task, event)`: task-only 原子变更+事件
  （现有 `mutate_task_and_stage` 强制要求 Stage 身份，不适合控制面）。
- 事件 payload: `{"previous_status": "...", "reason": ...}`（reason 可空，来自契约请求体）。
- 错误: Task 不存在 → `RecordNotFound`（Gateway 映射 `TASK_NOT_FOUND`）；
  状态不合法 → 新 `InvalidControlTransition`（Gateway 映射 `TASK_NOT_ACTIVE`）。
- 测试: `tests/backend/test_runtime_kernel_controls.py`，覆盖四命令 × 合法/非法状态、
  Lease 释放、事件顺序、终态守卫。

**验收**: 全量回归绿；控制面事件进入 task 事件流且 sequence 严格递增。

### Week 2 — Gateway 应用服务 ✅（2026-08-20，59 tests，回归 613）

`apps/console/backend/ai_game_console/gateway/` 新包（不 import 进 legacy `api.py` 逻辑）:
- **ConversationService**: 唯一关联规则（契约 §8）: 请求显式 `task_id` > 会话唯一 active task
  > 多 active → `CONVERSATION_CONFLICT`；不按最近消息/名称猜测。
- **IdempotencyService**: `Idempotency-Key` + payload 指纹持久化（SQLite 新表 + migration）；
  同键同 payload → 重放原响应；同键不同 payload → `IDEMPOTENCY_CONFLICT`。
- **TaskGateway**:
  - create → `kernel.create_task`（幂等）
  - get → **Gateway Snapshot 投影**（current task + active stage + last observation 摘要），
    不是 Store 行直出
  - message → Kernel 持久化（user 事件 / constraint）
  - control → `kernel.apply_control`，响应 `{accepted, task_id, command, status, event_sequence}`
- **投影规则**: Client Projection Event 不得制造 Runtime 中不存在的 Stage/Fact/完成状态（§13）。

**验收**: gateway 单测全绿；幂等重放语义有测试证明。

### Week 3 — HTTP API 表面 + 错误模型 ✅（2026-08-20，59 tests，回归 672）

- `gateway_api.py`: 独立 `APIRouter(prefix="/api/v1")`，实现契约全部端点:
  `GET /devices`、`POST /tasks`、`GET /tasks`、`GET /tasks/{task_id}`、
  `POST /tasks/{task_id}/messages`、`POST /tasks/{task_id}/controls`、
  `GET /tasks/{task_id}/events`（`after_sequence`+`limit` 分页，严格升序不重复）、
  `GET /tasks/{task_id}/observations/{observation_id}`、`POST /conversations/{conversation_id}/messages`
- **挂载策略**: `create_app(..., gateway: GatewayComposition | None = None)`（默认 None =
  关闭）。默认 app 组合与现状完全一致（legacy 回归不受影响，672 全绿证明）；cutover
  工单负责把默认切换为新 Gateway router。启用时 gateway router 在 legacy router **之前**
  注册 —— 共享路径按注册顺序解析到 gateway（TestClient 行为测试证明，FastAPI 此 checkout
  惰性包含 router，`app.routes` 无法断言顺序）。
- **错误模型**（契约 §14 十码）: `GATEWAY_ERROR_STATUS` 显式映射
  （400 VALIDATION/CURSOR、404 NOT_FOUND 族、409 CONFLICT/NOT_ACTIVE/NOT_AVAILABLE、
  403 LEGACY_TASK_WRITE_DISABLED、500 INTERNAL）；统一 `{"error": {code, message, retryable, details}}`；
  非 `GatewayError` 异常经 `_guarded` 归一为 `INTERNAL_ERROR`，traceback 不外泄。
- **请求体手动解析**: `_json_object_body` 经 `request.json()` 解析，任何解析错误/非对象
  → `VALIDATION_ERROR`（400），不产生 Pydantic 422 —— 绕过 legacy 对 `/api/v1/tasks*`
  的 RequestValidationError 特殊处理。
- 请求头: `Idempotency-Key`（create/message/control 必需）、`X-Client-Id`
  （create + 会话入口必需）；写端点复用既有 `X-AI-Game-Client: console-v1` 中间件
  （403 `console_client_required` 精确响应体有测试证明）。
- 设备: `AdbDeviceRegistry`（`runtime_adapters/android/device_registry.py`）按
  ADB 发现结果投影 `{"id", "availability"}`（READY→available、active 集合→in_use、
  其余→unavailable）；Gateway 服务层不直接触碰 ADB。
- 测试: `tests/backend/test_gateway_api.py`（59）—— TestClient 集成: 默认关闭、
  devices 投影、create/get 形状与幂等、message/control 形状 + 幂等重放 + 错误码、
  会话唯一关联（POST 入口 created/attach/conflict）、event 分页（after_sequence/limit/
  空页游标/未知 task 404）、observation 8 键投影、10 码错误模型、写头 403、
  未知 API 路由 404、gateway 对共享 canonical 路径的行为优先级、lifespan 关闭顺序。

**验收**: 契约 §17 非 SSE 项（第 1–5 项）通过；第 6 项 SSE 断线续传按计划属 Week 4；
legacy 全量回归不回归（672 passed，~131–161s）。

### Week 4 — SSE + 客户端投影 + 前端控制 UI

- `GET /tasks/{task_id}/events/stream?after_sequence=N`: SSE `id:`=sequence、
  `event: runtime_event`、data 为事件 JSON；心跳注释帧不占 sequence；
  慢 Client 不阻塞 Kernel（生成器背压隔离 + 断连清理）；投递失败不回滚已提交事件。
- 断线续传: 重连带最后成功 sequence + 重新 GET Snapshot 校准（§11）。
- 前端（`apps/console/web`）:
  - 任务卡片 pause/resume/cancel/takeover 按钮（按 status 可用性）
  - 事件增量消费（SSE + sequence 游标），投影为 `task_progress` 用户可读消息
  - takeover 态明确展示"用户接管中"投影（§9: 用户接管投影）
- 测试: SSE 续传单测（断开→带游标重连→无重复不丢失）；前端构建通过。

**验收**: 契约 §17 "SSE 断线续传"、"Snapshot + Event 校准"通过。

### 集成 — 验收 + 真机冒烟

- 契约 §17 全清单逐项核对（10 项）
- 场景 10 真机冒烟（有 adb 设备时执行；无设备则记录"待设备"并保持 Fake 证据）
- 全量回归；更新 `RUNTIME_KERNEL_ROADMAP_STATUS.md`（Phase 6 ✅ + 测试总数）
- 交付文档: `PHASE_6_WEEK_Y_*.md` 各周小结

## 3. 风险与边界

1. **共享路径冲突**（`/tasks` 族）: 不做混合挂载，cutover 单独工单。Phase 6 交付的是
   显式挂载的完整 Gateway，默认 app 不变。
2. **Schema 迁移**: Idempotency 新表 + 任何新表走既有 SQLite migration 机制，
   不破坏既有表。
3. **SSE 与 legacy 事件端点**（`GET /events`）并存: 契约 SSE 是 task 级
   `/tasks/{task_id}/events/stream`，与 legacy 全局 `/events` 不同路径，无冲突；
   cutover 工单再处理 legacy `/events` 的 deprecation/error。
4. **takeover 与 pause 的状态同构**: 两者都到 PAUSED，区别只在事件类型
   （`UserTakeover` vs `TaskPaused`）与客户端投影。Gateway 响应 `status` 都是 `PAUSED`，
   前端凭事件类型区分投影。

## 4. 里程碑

| 周 | 交付 | 验收 |
|---|---|---|
| W1 | Kernel 控制面 + 测试 ✅ | 全量回归绿（554） |
| W2 | Gateway 应用服务 + 测试 ✅ | gateway 单测绿（613） |
| W3 | 规范 HTTP API + 错误模型 + 测试 ✅ | §17 第 1–5 项（SSE → W4，回归 672） |
| W4 | SSE + 前端控制 UI + 测试 ✅ | §17 第 6–7 项（回归 679） |
| 集成 | §17 全清单 + 场景 10 + 回归 + 文档 ✅ | **Phase 6 ✅**（§17 10/10，场景 10 待设备，回归 后端 688 / 前端 52） |
