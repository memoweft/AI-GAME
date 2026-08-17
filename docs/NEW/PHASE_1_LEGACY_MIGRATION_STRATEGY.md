# Phase 1 — Legacy Migration Strategy

> 状态：FROZEN DESIGN  
> 日期：2026-08-10  
> 迁移执行状态：NOT STARTED

## 1. 目标

让 New Runtime Kernel 在不破坏现有证据和不产生双设备控制者的前提下，逐步替换旧 MobileTask 控制核心。

迁移不是：

- 把 `Subgoal` 重命名成 `Stage`；
- 把旧 Task row 直接复制到新表；
- 让新旧 Runtime 长期并行控制设备；
- 用 Legacy Adapter 维持第二套 Active Task 状态机；
- 为了兼容而伪造新 Fact、Verification 或 Checkpoint。

迁移目标：

```text
Current
  ├── Old MobileTaskRuntime → ADB
  ├── Chat device loop      → ADB
  ├── Game Learning         → ADB
  ├── direct executor API   → ADB
  └── Application/Soul      → external owner

Target
  ├── Clients → Runtime Gateway → New Runtime Kernel → Device Port
  ├── Legacy history → read-only Legacy Archive
  └── Legacy intents → explicit Gateway translation or rejection
```

## 2. 已冻结策略

1. 方案 B：同一工程内建立独立 Runtime Kernel 与 Store。
2. 旧 `runtime/console/mobile-tasks.db` 在 cutover 时 Freeze，不直接迁移。
3. 新数据写入 `runtime/console/runtime.db`。
4. 旧 TaskPlan、Subgoal、Reflection、SkillMemory 不进入 New Runtime domain。
5. New Runtime 激活后，Legacy 不能直接控制 Android。
6. 新旧 API 不通过同一路径的 payload 猜测来区分。
7. 旧数据保留可审计性；不为兼容修改旧历史事实。
8. 迁移按可验证阶段进行，每阶段独立 STOP。

## 3. Legacy 范围

### 3.1 Old MobileTask

代码：

```text
ai_game_console/mobile_agent/
ai_game_console/mobile_task_adapter.py
ai_game_console/mobile_task_profiles.py
```

数据：

```text
runtime/console/mobile-tasks.db
runtime/sessions/mobile-tasks/evidence/
```

API：

```text
POST/GET /api/v1/tasks
GET /api/v1/tasks/{task_id}
POST /api/v1/tasks/{task_id}/inputs
POST /api/v1/tasks/{task_id}/stop
```

### 3.2 其他可能控制设备的旧路径

- Chat cloud-execute/local device loop；
- Game Learning Android Adapter；
- `POST /api/v1/executor/actions` direct executor route；
- 任何直接构造 `AdbGuiExecutor` 的旧服务；
- 可能通过外部 owner 控制同一物理设备的 Soul/dating-copilot dispatch。

这些路径即使不使用 `mobile-tasks.db`，也在 Device Ownership cutover 范围内。

### 3.3 非设备兼容数据

Workflow、Run、Approval、Chat history、Learning history 和 ApplicationRuntime history 可以继续作为兼容/只读产品数据存在，但不得被描述为 New Runtime Task Fact 或 Event。

## 4. 能力处置表

| Legacy 能力 | 处置 | 目标语义 |
|---|---|---|
| 旧 Task list/detail history | 保留为只读 Archive | 原样展示旧状态与旧字段，明确标记 legacy |
| 旧 Task 创建 | cutover 后废弃 | Client 改用 New `POST /api/v1/tasks` contract |
| 旧 `/inputs` | cutover 后废弃 | Client 改用 `/messages`；不静默改写旧 revision 语义 |
| 旧 `/stop` | cutover 后废弃 | Client 明确调用 `/controls` 的 cancel/pause/takeover；不把 stopped 偷换成 CANCELLED |
| 旧 TaskPlan/Subgoal | 仅旧历史保留 | 不转换为 New Stage |
| 旧 Attempt/Verification | 仅旧历史保留 | 不自动晋升为 New Action/Fact |
| 旧 Reflection/SkillMemory | 仅旧历史保留 | 不进入 v0.1 Context 或新 Store |
| ADB discovery/executor | 复用基础设施 | 通过 New Device Adapter/Port 使用 |
| DeviceExecutionLease | 复用排他机制 | Runtime Kernel 成为唯一申请核心 |
| GUI-Owl client | 复用传输 Adapter | 通过 Planner/Operator/Language Router 绑定 |
| FastAPI/Vite/Test infrastructure | 保留 | 新 Gateway/Workbench 逐阶段接入 |
| Legacy device writers | 禁用或改为高层 Gateway Client | 不再持有 ADB/Lease |
| Soul ApplicationRuntime | 暂时隔离 | 未来 Soul 作为 Gateway Client，不作为 Mobile Runtime |

## 5. 为什么不直接迁移旧数据

旧数据表达：

```text
Goal
→ complete TaskPlan
→ ordered Subgoals
→ ActionAttempt
→ Reflection
→ SkillMemory promotion
```

新数据表达：

```text
Goal
→ one Current Stage
→ Observation
→ one Action + Expected Outcome
→ ActionExecution
→ Verification
→ Commit Fact/Stage progress
→ Event/Checkpoint
```

直接迁移会产生无法证明的转换：

- 一个旧 Subgoal 是否等于一个新 Stage；
- 一个旧 verified attempt 是否符合新 Expected Outcome/Fact 来源契约；
- 旧 `uncertain` 是 Task 终态还是 Verify verdict；
- 旧 SkillMemory 是否允许进入新 MVP；
- 旧 stop/restart 是否能映射为 PAUSED/CANCELLED/FAILED。

因此冻结旧库比编造等价关系更可信。

## 6. Legacy Archive

Legacy Archive 是只读能力，不是 Runtime：

```text
Legacy Archive Service
   ├── open mobile-tasks.db read-only
   ├── list legacy tasks
   ├── inspect legacy task
   ├── show original status/plan/attempts/events
   └── never create/input/stop/dispatch
```

要求：

- 数据库使用 read-only 打开方式；
- 不运行旧 schema migration；
- 不更新 last_seen 或其他字段；
- 响应明确包含 `source=legacy` 和旧 contract version；
- 不把旧记录混入 New Task list；
- evidence 缺失时明确显示 retention 状态，不伪造预览；
- Archive 故障不影响 New Runtime 运行。

推荐的未来只读 HTTP namespace：

```text
GET /api/v1/legacy/tasks
GET /api/v1/legacy/tasks/{task_id}
```

精确路径在 cutover API 施工单中实现；Phase 1 不修改现有 route。

## 7. Legacy Adapter

Legacy Adapter 只处理两类输入：

### 7.1 只读旧历史

转交 Legacy Archive，完全不进入 Kernel。

### 7.2 明确支持的高层意图转换

只有在单独兼容施工单逐项定义并测试后，旧 Client 意图才可以转换为 New Gateway 命令，例如：

```text
legacy create intent
  → canonical CreateTask command

legacy additional user text
  → canonical UserMessage command
```

转换必须满足：

- 目标 New Task ID 明确；
- idempotency key 可稳定继承；
- 不丢失用户原文；
- 状态语义等价且可向 Client 明示；
- 不生成旧 plan/subgoal/reflection；
- 不触碰 Device Port。

默认 fail-closed。以下操作不允许隐式转换：

- old `stop` → new `cancel`；
- old `uncertain` → new `FAILED` 或 `STUCK`；
- old Subgoal completed → new StageCompleted；
- old SkillMemory → new Fact/Checkpoint；
- 缺少 conversation/task 唯一关联的 message。

## 8. `/api/v1/tasks` 路由切换

现有与新系统都需要 `/api/v1/tasks`，不能长期同时占用不同语义。

冻结的迁移方式是原子 contract cutover：

### cutover 前

- 现有 `/api/v1/tasks` 保持旧行为；
- New Kernel 可以在内部完成 domain/store 测试，但不对外宣称 canonical Gateway 已启用；
- 不按 Header、payload 或 task id 猜测 route 版本。

### cutover 窗口

- 拒绝新 legacy device work；
- drain 已下发动作并关闭旧 worker；
- 生成旧数据库和 evidence retention 状态证据；
- 将旧 store 置为只读；
- 验证所有 legacy device writers 不再持有目标设备；
- 启用 New Gateway canonical contract。

### cutover 后

- `/api/v1/tasks` 只返回 New Runtime schema；
- `/inputs` 和 `/stop` 固定返回 `410` / `LEGACY_TASK_WRITE_DISABLED`，并给出新契约方向；
- 旧历史只通过显式 legacy archive namespace 查看；
- 旧 Client 必须升级，不能获得伪造兼容响应。

这个 cutover 是未来公共 API 变更，必须由独立施工单完成 API tests、客户端迁移和回滚演练。

## 9. 迁移阶段

### M0 — Design（本阶段）

- 冻结边界、数据、Gateway、Legacy、Device Ownership 文档；
- 不改代码和运行状态。

### M1 — Kernel Skeleton, INACTIVE

- 创建纯 domain/event/store skeleton；
- 不装配 Device Adapter；
- 不注册 canonical public routes；
- 不申请 Lease。

### M2 — Store/API Contract Tests, INACTIVE

- 使用新临时数据库测试；
- 测试 Snapshot/Event/Idempotency；
- Legacy 当前行为保持不变；
- 不操作 Android。

### M3 — Device Adapter Seam, EXCLUSIVE TEST

- Kernel 通过 Device Port 连接复用的 ADB 基础设施；
- 只在明确隔离的测试设备或旧 writer 全部禁用时验证；
- 证明 lease 排他和 no-replay；
- 不并行运行 Legacy device work。

### M4 — Gateway/Client Migration, PRE-CUTOVER

- 实现新 messages/controls/events/SSE；
- 更新 Web Client；
- 完成新旧 API 契约测试；
- 准备 Archive 与 410 行为；
- 尚不切换生产设备所有权。

### M5 — DRAIN/FREEZE/CUTOVER

- 执行 Device Ownership 的 DRAINING；
- 冻结旧库；
- 禁用所有 legacy device writer；
- 激活 New Kernel ownership；
- canonical `/api/v1/tasks` 切换；
- 保存 cutover 证据。

### M6 — Legacy Retirement

- 观察一段受控运行期；
- 只读 Archive 保留；
- 根据 Owner 决定旧代码与兼容 UI 的退役时间；
- 删除任何旧资产都需要另行授权。

完成一个 M 阶段必须 STOP，不自动进入下一个阶段。

## 10. 旧前端策略

Phase 1 不改前端。未来迁移规则：

- 当前 MobileTaskWorkspace 在 cutover 前继续配合旧 API；
- 新 Chat Workbench 通过 New Gateway 开发和测试；
- cutover 前必须完成前端状态词、消息、控制、Snapshot/Event/SSE 适配；
- 旧 Task 历史使用明确的 Legacy History 入口；
- 不在同一组件中把旧 Subgoal 和新 Stage 混合显示；
- 前端本地状态不能决定 Task 是否完成。

## 11. Soul / ApplicationRuntime 策略

冻结方向：

```text
Soul personality/preference/agent
             │
             ▼
      Runtime Gateway Client
             │
             ▼
      New Runtime Kernel
```

当前 ApplicationRuntime 与 dating-copilot owner 集成在 Phase 1 不改动，但它属于隔离的 Legacy 应用路径，不等于新 Soul Gateway Client 已实现。

在 New Kernel 管理同一物理设备之前，必须完成以下之一：

1. 将 Soul 设备意图迁移为 Gateway Task/Message；或
2. 保证外部 owner 使用不同 device_id 且有可验证隔离；或
3. 禁用该外部 physical dispatch。

不能仅因 AI-GAME 本地 lease 可用，就假定 `F:\dating-copilot` 外部 owner 不会操作同一设备。

长期人格、用户偏好和跨 Task Memory 仍不在 v0.1 实现范围；“Soul 是人格层”只冻结职责位置。

## 12. Chat / Learning / Direct Executor 策略

### Chat

纯文本 Chat 可继续作为兼容能力。任何 device-execute Chat path 在 Kernel ownership 激活后必须成为 Gateway Client 或被禁用。

### Game Learning

历史与 PolicyMemory 可以保留，但 Android Adapter 不得与 Kernel 同时控制同一设备。Learning 的未来设备采集必须通过 Kernel 授权的专门任务边界，且不在 v0.1 MVP 范围。

### Direct Executor API

`POST /api/v1/executor/actions` 在 Kernel ownership 激活后不能继续成为受管设备的旁路。候选处置只有：禁用、限制到明确不受管的测试 target，或转换为 Kernel 内部测试端口；不得默认继续。

## 13. 回滚边界

回滚不能破坏 no-replay：

- 如果 New Kernel 尚未向设备发送动作，可以停用新装配并恢复旧服务配置；
- 如果 New Kernel 已发送动作，必须先结算/观察其真实状态，不能直接重启 Legacy worker；
- 新 `runtime.db` 和旧 frozen `mobile-tasks.db` 不合并；
- 回滚不把新 Event 写回旧库；
- 重新启用旧 device writer 前必须确认 New Kernel Lease 已释放且不存在 open physical intent；
- 任何无法确认的设备状态进入人工检查，不自动补发。

## 14. 迁移证据包（未来）

正式 cutover 至少保存：

- 旧 writer 列表及禁用证据；
- 旧活动 Task drain 结果；
- frozen DB 路径、大小、时间和内容 hash；
- 新 DB schema revision；
- Device Lease owner 切换证据；
- API contract tests；
- Legacy Archive read-only tests；
- New Gateway create/message/control/SSE tests；
- no-replay/crash recovery tests；
- 真机动作前后的独占证据；
- 回滚演练结果；
- Owner 是否接受 cutover 效果。

## 15. Phase 1 未执行事项

- 未冻结或复制旧数据库；
- 未创建 `runtime.db`；
- 未修改 route；
- 未禁用旧 worker；
- 未切换 lease owner；
- 未修改前端；
- 未迁移 Soul；
- 未操作 Android；
- 未退役或删除任何 Legacy 文件。

## 16. 状态

`DESIGN FROZEN — MIGRATION NOT STARTED`
