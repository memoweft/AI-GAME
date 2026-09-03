# WeftMate / DSH V2 执行契约

## 所有权

DSH 拥有对话、session、turn、tool call、权限、审批和持久工具结果。AI-GAME 拥有 Android Task、revision、Profile、调度、观察、动作、验证、等待、恢复、事件和执行经验。

正式 owner 是经过 host 认证的：

```text
{principal_id, controller_id}
```

它不来自 bearer token、DSH session 或请求 body（请求体）。同一完整 pair 可跨 DSH 会话读取和控制自己的 Task；任一字段不同都不得看到对象是否存在。

## 新任务入口

基址为 `/api/execution/v2`。主要接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 能力与版本事实 |
| `POST` | `/tasks` | 幂等创建或恢复同一 canonical Task |
| `GET` | `/tasks` | 列出当前 owner 的 Task |
| `GET` | `/tasks/{task_id}` | 读取权威 Task 投影 |
| `GET` | `/tasks/{task_id}/events` | 游标事件读取 |
| `POST` | `/tasks/{task_id}/revisions` | 增补或修改目标 |
| `POST` | `/tasks/{task_id}/controls` | 暂停、继续、接管、释放或取消 |
| `POST` | `/tasks/{task_id}/answers` | 回答当前结构化问题 |
| `POST` | `/tasks/{task_id}:archive` | 归档终态 Task |
| `GET/POST` | `/device-profiles...` | owner-scoped 模拟器 Profile 管理 |
| `GET` | `/tasks/{task_id}/experience` | 安全经验投影 |

新 submit 只接受用户目标、DSH 来源身份、选定的模拟器 Profile 和 host 已完成的 authorization mode（授权模式）。受信 host 冻结唯一正式 runner：`android_ui_agent/1`；其他 runner 不进入新任务。

## 认证

请求同时需要：

- `X-AI-Game-Client` 固定客户端身份；
- `Authorization: Bearer <capability>`；
- `X-AI-Game-Principal-Id`；
- `X-AI-Game-Controller-Id`。

capability 只用于认证，owner IDs 来自受信 WeftMate host context（宿主上下文）。它们不得进入 URL、renderer、普通日志或错误回显。

## 幂等与控制

- 同一 DSH user turn（用户轮次）和 owner pair 的重试返回同一 submission/Task，不因 tool attempt（工具尝试）变化创建第二个 Task。
- Task revision、control 与 effect-bearing dispatch（产生效果的派发）必须在 canonical store（权威存储）边界竞争；用户控制获胜后，迟到结果不得覆盖。
- DSH turn/tool abort 只结束当前 HTTP 等待或 watcher（观察器），不取消长期 Task。
- 只有明确 `cancel` control 才取消。

## 状态与证据

公开状态可以包含 `scheduled`、`running`、`waiting_time`、`waiting_event`、`recovering`、`replanning`、`needs_user_input`、`paused`、`user_takeover` 和终态。没有可信分母时进度为 unknown（未知），不生成百分比。

成功必须由当前 revision、owner、Profile、设备身份、runner binding（执行器绑定）、新鲜 observation、动作回执和完成判据验证共同支持。原始截图路径、ADB locator（定位符）、token、owner ID、用户秘密和未脱敏模型 reason（理由）不进入公共 Task/event 投影。

普通失败保持可恢复或等待。重复真实副作用、身份/设备串线、凭据暴露或伪造成功才进入完整性阻断。

## V1 边界

`/api/execution/v1` 只为既有记录的读取、诊断和迁移保留。它不能创建或控制新正式任务，也不得成为 WeftMate 的 fallback（后备路径）。
