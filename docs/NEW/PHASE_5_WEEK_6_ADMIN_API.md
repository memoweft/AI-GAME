# Phase 5 Week 6：管理 UI/API（Lease 状态查询与手动干预）

> 状态：✅ 已完成
> 日期：2026-08-19
> 前置：Week 4 设备独占（Lease + 后台清理）、Week 5 Deadline 保护已完成（Schema v5）

## 1. 目标

为 Lease 生命周期提供**只读查询**与**手动干预**能力，使运维/调试可以：

- 查看当前所有 Lease（按设备/任务过滤）、单个 Lease 详情、总体统计；
- 强制释放某个 Lease（如持有进程已死但清理尚未跑到的窗口期）；
- 手动触发一次清理扫描（不必等待 30s 后台周期）。

## 2. 架构

### 2.1 组成（`runtime_admin.py`）

```
create_app(settings=...)
   └─ LeaseAdminService(data_dir/runtime/runtime.db)   # 懒初始化
         ├─ SQLiteRuntimeStore(runtime.db)             # 与 Kernel 共用同一数据库
         └─ DeviceLeaseManager(store, real_clock)     # 复用 Week 4/5 管理器
```

- **懒初始化**：`create_app` 只构造 `LeaseAdminService` 对象（无文件副作用），
  首次请求管理端点时 `ensure_ready()` 才建目录、开库、`initialize()`（已存在则幂等 no-op）。
  这样不触碰管理端点的既有 `create_app` 测试不会新增任何数据库文件。
- **共用 runtime.db**：管理面与 Kernel 运行面读同一份 `runtime_device_leases` 表，
  查询永远反映真实状态，无缓存、无第二份数据。
- **后台清理线程**：默认随首次 `ensure_ready()` 启动（`interval=30s`，Week 4 能力）；
  测试通过 `background_cleanup=False` 关闭。`shutdown()` 停止线程并关闭连接（幂等）。
- **生命周期**：`create_app` 的 lifespan 关闭阶段把 `admin.shutdown()` 加入 best-effort
  清理回调，与其他组件（chat coordinator、mobile task runtime 等）同一模式。

### 2.2 路由挂载与写保护

- 路由前缀 **`/api/v1/runtime/leases`**（Roadmap 字面写的 `/api/runtime/leases`
  未带版本前缀；console 所有业务 API 统一在 `/api/v1` 下，此处按既有约定挂载）。
- 写保护中间件按 `request.url.path` 前缀匹配 `POST + /api/v1/*`，
  因此管理端点的 POST 自动要求 `X-AI-Game-Client: console-v1` 头，
  与 console 其他写接口同一策略（缺失返回 403 `console_client_required`）。

## 3. API 规范

统一时间戳：ISO 8601 UTC（`+00:00` 偏移）。错误载荷统一顶层
`{"error": {"code", "message"}}`（与 console 其余 API 一致）。

### 3.1 `GET /api/v1/runtime/leases`

查询参数（均可选）：`device_id`、`task_id`（同时提供则取交集）。

```json
{
  "now": "2026-08-19T05:30:00+00:00",
  "count": 1,
  "leases": [
    {
      "lease_id": "lease-1",
      "device_id": "device-1",
      "task_id": "task-1",
      "holder_process_id": "12345",
      "acquired_at": "2026-08-19T05:29:00+00:00",
      "expires_at": "2026-08-19T05:30:00+00:00",
      "deadline_at": "2026-08-19T05:34:00+00:00",
      "last_heartbeat_at": "2026-08-19T05:29:50+00:00",
      "action_id": null,
      "status": "active",            // active | expired（相对 now）
      "deadline_exceeded": false,    // 相对 now
      "holder_process_alive": true   // 持有进程当前是否存活
    }
  ]
}
```

### 3.2 `GET /api/v1/runtime/leases/stats`

```json
{
  "now": "2026-08-19T05:30:00+00:00",
  "stats": {
    "total": 2,
    "active": 1,
    "expired": 1,
    "deadline_exceeded": 1,
    "avg_current_hold_seconds": 200.0   // 无当前 Lease 时为 null
  },
  "cleanup": { "runs": 3, "errors": 0 }
}
```

> 路由注册顺序：`/stats` 必须先于 `/{lease_id}`，避免被路径参数吞掉（有测试覆盖）。

### 3.3 `GET /api/v1/runtime/leases/{lease_id}`

返回与列表相同结构的单个 Lease 载荷；不存在时 `404 {"error":{"code":"lease_not_found"}}`。

### 3.4 `POST /api/v1/runtime/leases/{lease_id}/release`

手动强制释放（需写保护头）。行为与后台清理一致：
若 Lease 关联了 Action，先创建恢复 Checkpoint 再删除 Lease 行。

```json
{
  "lease_id": "lease-1",
  "found": true,
  "released": true,
  "checkpointed": false,
  "error": null
}
```

- `404`：Lease 不存在（`lease_not_found`）；
- `500`：释放过程异常（`lease_release_failed`，`error` 携带原因）。

### 3.5 `POST /api/v1/runtime/leases/cleanup`

手动触发一次完整清理扫描（需写保护头），立即返回摘要：

```json
{
  "expired_found": 1,
  "deadline_exceeded": 1,
  "released": 1,
  "checkpointed": 0,
  "errors": 0
}
```

语义与 `DeviceLeaseManager.trigger_cleanup()` 完全一致：
超 Deadline 的 Lease 即使持有进程存活也强制回收并记 `deadline_exceeded`。

## 4. 运维手册

**查看当前设备占用**

```bash
curl -s http://127.0.0.1:5000/api/v1/runtime/leases | jq
curl -s "http://127.0.0.1:5000/api/v1/runtime/leases?device_id=emulator-5554" | jq
```

**总体健康**

```bash
curl -s http://127.0.0.1:5000/api/v1/runtime/leases/stats | jq
# stats.active / stats.deadline_exceeded / cleanup.errors 是三个核心指标
```

**强制释放卡死的 Lease**（如 `holder_process_alive=false` 但清理线程尚未跑到）

```bash
curl -s -X POST http://127.0.0.1:5000/api/v1/runtime/leases/<lease_id>/release \
  -H "X-AI-Game-Client: console-v1" | jq
```

**立即触发清理**（不等 30s 周期）

```bash
curl -s -X POST http://127.0.0.1:5000/api/v1/runtime/leases/cleanup \
  -H "X-AI-Game-Client: console-v1" | jq
```

**注意事项**

- 管理面是"旁路观察 + 手动干预"，不改变 Kernel 运行面的任何行为；
  运行面仍按 Week 4/5 语义自治（续期钳制、Deadline 强制回收、进程崩溃恢复）。
- 手动释放与后台清理可能并发：`force_release` 对不存在的 Lease 返回 `found=false`，
  清理对已被释放的 Lease 幂等跳过，二者互不冲突。
- 数据库位于 `data_dir/runtime/runtime.db`（默认 `<project>/apps/console/data` 同级，
  由 `Settings.data_dir` 决定）。删除该文件等同于"忘记所有 Lease"，
  关联 Action 的恢复 Checkpoint 仍在任务表内，Kernel 可继续恢复。

## 5. 测试

`apps/console/tests/backend/test_runtime_admin_api.py`（9 个集成测试，TestClient）：

- 懒初始化：首次请求前无数据库文件，请求后建立；
- 列表：状态字段（active/expired/deadline_exceeded/holder_process_alive）正确性；
- 过滤：`device_id` / `task_id` / 组合过滤；
- 详情：200 载荷 + 404 `lease_not_found`；
- 统计：`/stats` 不被 `/{lease_id}` 吞掉，计数与 `avg_current_hold_seconds` 正确；
- 写保护：POST 无头 → 403 `console_client_required`（release 与 cleanup 均覆盖）；
- 手动释放：删行、无 Action 时 `checkpointed=false`、释放后详情 404、未知 lease 404；
- 手动清理：过期且超 Deadline 的 Lease（死进程）被释放，`runs` 计数递增，`errors=0`。

> 管理端点使用**真实时钟**，因此测试种子数据按真实 now 偏移生成
> （active = now 获取 + 1h TTL；expired = 400s 前获取 + 60s TTL，默认 300s Deadline 已超）。

## 6. 与 Roadmap 的偏差

| Roadmap 项 | 实现 |
| --- | --- |
| `GET /api/runtime/leases...` | `GET /api/v1/runtime/leases...`（统一版本前缀） |
| 前端页面（可选） | 未做（API 先行；前端为 Roadmap 标注的可选项） |
| 权限控制（管理员操作） | 复用 console 写保护头（`X-AI-Game-Client`），未引入独立管理员身份 |
