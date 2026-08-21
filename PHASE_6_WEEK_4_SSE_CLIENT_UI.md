# Phase 6 Week 4: SSE 事件流 + 客户端投影 + 控制 UI

> **历史 Foundation 施工证据，不是当前产品路线图。** 当前权威规范见 [`docs/product/00_INDEX.md`](docs/product/00_INDEX.md)。

**日期**: 2026-08-20
**契约依据**: `docs/NEW/PHASE_1_GATEWAY_CONTRACT_DESIGN.md` §10/§11/§13（DESIGN FROZEN）
**前置**: Week 3 完成（规范 HTTP API + 错误模型，回归 672 passed）

---

## 1. 交付内容

### 1.1 SSE 事件流（后端，契约 §11）

`GET /api/v1/tasks/{task_id}/events/stream?after_sequence=N`
（`apps/console/backend/ai_game_console/gateway_api.py`）

- **冻结帧形**: `id: <sequence>` + `event: runtime_event` + `data: {"sequence", "type", "payload"}`
  （严格三键，不是 §10 分页形状）+ 空行结束。
- **游标语义**: `after_sequence` 缺省 0；非法游标（非整数/负数）在流开启**前**返回 400
  `EVENT_CURSOR_INVALID` JSON，未知 task 返回 404 `TASK_NOT_FOUND` —— 绝不产生"半开"的坏流。
- **心跳**: `: heartbeat` 注释帧，不是 Runtime Event、不占 Task sequence（每
  `heartbeat_interval` 秒一帧，测试组合缩至 20ms 验证）。
- **慢 Client 隔离**: 生成器是纯只读轮询（每 `poll_interval` 读一次
  `kernel.events(task_id, after_sequence=cursor)`）——只读已提交事件日志，
  永不阻塞 Kernel；客户端慢只影响它自己的投递节奏。
- **断连清理**: 客户端断开时生成器捕获 `GeneratorExit` 并退出，无残留轮询任务。
- **投递失败不回滚**: 事件一旦经 Kernel 提交即不可撤回；客户端错过的事件
  通过带游标重连 + 重新 GET Snapshot 校准找回（§11）。
- 响应头: `text/event-stream` + `Cache-Control: no-cache`。

**为什么用真服务器测试**: starlette TestClient 会阻塞其 transport 直到 ASGI app
调用*完成* —— §11 流永不完成，所以 `tests/backend/test_gateway_sse.py` 用
uvicorn（daemon 线程 + 随机空闲端口）走真实 socket。所有任务活动都经 HTTP
表面（create → message → cancel），Kernel 写入全部落在服务器线程。

### 1.2 客户端事件流封装（前端 `src/gateway/eventStream.ts`）

- 浏览器 EventSource 的自动重连**不可用**（服务端忽略 Last-Event-ID 头），
  所以客户端自己持有游标：断线后 `connect(afterSequence)` 显式带
  `after_sequence` 重开。
- **游标单调递增**: 只有 `sequence > cursor` 的帧被接受（去重服务端在每次新
  连接上的回放），`connect()` 永远只能推进游标、不能回退。
- 心跳注释帧天然到不了 message 监听器，零游标影响。
- `parseSseData()` 严格校验冻结三键帧，任何畸形帧静默忽略、连接保持。
- `calibrateCursor(local, snapshot.last_event_sequence) = max(两者)` ——
  §11 Snapshot/Event 校准，永不回退。

### 1.3 用户可读投影（前端 `src/gateway/projection.ts`，契约 §13）

- 每个 Runtime 事件 → 一行用户可读中文（TaskCreated / StageStarted /
  ActionProposed / ActionVerified / TaskPaused / UserTakeover …），只读事件
  payload 中 Kernel 实际发出的字段；缺字段降级为通用措辞，**绝不制造**
  Runtime 中不存在的 Stage/Fact/完成状态。
- 未知事件类型诚实降级为 `任务事件：<type>`。
- 长字段截断 120 字符 + 省略号。
- `isUserTakeover(events)`: Kernel 把 pause 和 takeover 都映射到 PAUSED，
  投影用"最新暂停族事件（TaskPaused/TaskResumed/UserTakeover）是否为
  UserTakeover"区分接管态 —— takeover 投影不凭别的东西推断。

### 1.4 任务流 Hook（前端 `src/gateway/useGatewayTaskStream.ts`）

每个选中任务的完整生命周期:

1. GET §6 Snapshot（状态徽标、当前阶段、控制按钮可用性）;
2. 从 sequence 0 经 §10 分页端点**逐页回填**完整事件历史（驱动进度时间线）;
3. 在 `calibrateCursor(回填游标, snapshot.last_event_sequence)` 处打开 §11 SSE;
4. 断线: 重新 GET Snapshot → 校准游标 → 回填缺口 → 固定延迟 2s 重连，
   上限 8 次；全程可见状态"流已断开 / 重连中";
5. 终态任务（SUCCEEDED/CANCELLED/FAILED）不开流。

### 1.5 控制 UI（前端 `src/components/GatewayTaskWorkspace.tsx`）

- **任务卡片**: 状态徽标 + pause / resume / cancel / takeover 四个控制按钮，
  按 Snapshot `status` 可用性启用（RUNNING → pause/takeover/cancel；
  PAUSED → resume/cancel；终态全禁）。
- **takeover 态**: 事件流判定接管时显示醒目横幅"用户接管中，任务已暂停 —
  请完成手动操作后点击恢复"，状态徽标同步切换为"用户接管中"（warning 色）。
- **事件增量消费**: SSE 帧经投影模块变为 `task_progress` 用户可读消息，
  追加到"任务进展"时间线（上限 50 条展示），技术视图保留原始事件（100 条）。
- **流健康**: 显示"已连接 / 重连中 / 已断开（n 次）"，断连次数超过上限后
  停止自动重连并提示手动刷新。
- 发送控制命令携带 `Idempotency-Key`（`newIdempotencyKey()`），失败保留可重试。

### 1.6 状态元数据（`src/status.ts`）

`gatewayTaskStatusMeta(status, { userTakeover })`: PAUSED + 接管 →
`用户接管中 / 用户已接管设备操作，任务保持暂停`（warning）；其余走
GATEWAY_TASK_STATUS 表。

---

## 2. 测试

### 后端 `tests/backend/test_gateway_sse.py`（7 tests）

| 测试 | 断言 |
|---|---|
| `test_stream_frozen_framing_and_payload_shape` | 帧形三键 + id/event/data 行 + 空行结束 |
| `test_stream_response_headers` | `text/event-stream` + `no-cache` |
| `test_stream_resume_after_disconnect_no_duplicates_no_gaps` | 断开 → 带最后游标重连 → 只收到新事件，无重复无丢失 |
| `test_stream_reconnect_from_zero_replays_full_log` | 从 0 重连回放完整日志 |
| `test_heartbeat_is_comment_frame_and_consumes_no_sequence` | 心跳是注释帧、不占 sequence，下一个真实事件编号连续 |
| `test_stream_unknown_task_404_pre_stream` | 未知 task → 404 TASK_NOT_FOUND（流开启前） |
| `test_stream_invalid_cursor_400_pre_stream` | 非法游标 → 400 EVENT_CURSOR_INVALID（流开启前） |

### 前端（13 tests，`tests/frontend/`）

- `gatewayEventStream.test.ts`（8）: 游标单调递增去重、connect 只推进不回退、
  非法游标拒绝、断线关闭源 + onDrop + 按游标重开、畸形帧忽略、teardown 后
  无回调、parseSseData 三键严格校验、calibrateCursor 取 max。
- `gatewayProjection.test.ts`（5）: 已知事件 → 可读文本、未知类型诚实降级、
  长字段截断、字段透传、takeover 判定仅凭最新暂停族事件。

**前端全量**: 52 passed（9 文件，含既有 39 + 新增 13）。
`App.test.tsx` 导航断言同步加入"网关任务"（Week 3 挂载的导航项，旧断言
未覆盖导致回归失败，已修正）。

**前端构建**: `tsc --noEmit && vite build` 通过
（dist: index-*.js 286 kB / gzip 87 kB）。

### 全量回归

**后端 679 passed**（672 + 7 新 SSE），~167s。

---

## 3. 验收（契约 §17）

| §17 项 | 状态 | 证据 |
|---|---|---|
| SSE 断线续传 | ✅ | `test_stream_resume_after_disconnect_no_duplicates_no_gaps` + 前端游标去重测试 |
| Snapshot + Event 校准 | ✅ | `calibrateCursor`（max 语义，测试覆盖）+ Hook 断线重连流程 |

Week 4 验收通过。剩余 §17 项（Web/Hermes 共享语义、Client 无法访问 ADB、
legacy write 不绕过 Kernel）在集成阶段逐项核对。

---

## 4. 边界与决策

1. **TestClient 不能跑 SSE**: starlette TestClient 阻塞至 ASGI 调用完成，
   SSE 流永不完成 —— 故 SSE 测试走真实 uvicorn socket（daemon 线程 +
   随机端口），其余端点仍用 TestClient。
2. **服务端忽略 Last-Event-ID**: 游标只走 `after_sequence` 查询参数，
   客户端显式管理 —— 不依赖浏览器自动重连（无法正确续传）。
3. **takeover 与 pause 状态同构**: 两者都是 PAUSED，UI 凭事件类型区分
   （§13 投影规则），不新增第二套状态。
4. **回填 + 流双通道**: 历史走 §10 分页（可断点续读、可单测），实时走 §11
   SSE；两通道以"游标只进不退"合并，任何重放都被去重。
