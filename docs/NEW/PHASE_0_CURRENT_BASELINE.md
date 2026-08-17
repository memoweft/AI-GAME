# Phase 0 — 现状与工程基线

> 日期：2026-08-10  
> 工作区：`F:\AI-GAME`  
> 依据：`docs/NEW/docs/00_README.md` 至 `20_OPEN_QUESTIONS_AND_NON_GOALS.md`  
> 状态：`DONE — STOP`  
> 本文只记录现状与证据，不决定 Phase 1 的迁移切口。

## 1. 本施工单

### TASK

形成当前仓库的可复核现状地图与运行、测试基线。

### IN SCOPE

- 识别后端、前端、ADB、模型、持久化与 Soul 集成入口；
- 记录仓库状态、启动和测试方法；
- 只读检查当前服务、模型、API 与 Android Target；
- 标出相对新设计基线可复用的部分和职责冲突。

### OUT OF SCOPE

- 修改产品行为、公共 API、数据模型或运行时状态机；
- 停止、重启或替换当前控制台、模型服务与设备执行器；
- 向 Android 设备发送动作；
- 决定现有 MobileTask 是迁移、并存还是替换；
- 开始 Phase 1 或后续阶段。

## 2. 工作区状态

- `F:\AI-GAME` 及其已枚举子目录中没有 `.git`，因此无法记录 Git branch、HEAD 或 tracked/untracked diff。
- 未发现仓库级 `AGENTS.md`。
- 当前目录已有运行产物、SQLite 数据库、日志、截图、模型缓存与前端依赖；这些均被视为用户现有资产，没有清理、重置、提交或推送。
- 本次新增的唯一项目文件是本报告；自动测试还可能刷新 `.pytest_cache` / `.ruff_cache` 等工具缓存。

## 3. 当前技术栈与入口

### 3.1 后端与 Gateway 现状

- 后端位于 `apps/console/backend`，是 Python 3.11+、FastAPI、Uvicorn 应用。
- ASGI 入口为 `ai_game_console.app:app`，应用装配集中在 `ai_game_console.api.create_app()`。
- 当前 FastAPI 服务已经是 Web 控制台的统一 HTTP 入口，但资源面远大于新 v0.1 草案：除 Task 外还包含 Application、Chat、Learning、Run、Workflow、Approval、Soul compatibility、Cloud Settings 与直接 Executor API。
- 当前 OpenAPI 中与 MobileTask 直接相关的接口是：
  - `POST/GET /api/v1/tasks`
  - `GET /api/v1/tasks/{task_id}`
  - `POST /api/v1/tasks/{task_id}/inputs`
  - `POST /api/v1/tasks/{task_id}/stop`
- 当前没有新草案要求的 Task `messages`、统一 `controls`、按 Task 增量 `events`、SSE `events/stream` 和 Task Observation 预览接口。
- 当前前端 API 基址已经是 `/api/v1`，可复用版本前缀和同源部署方式。

### 3.2 当前 MobileTask Runtime

- 核心实现位于 `ai_game_console/mobile_agent/`：
  - `domain.py`：Observation、PhysicalIntent、Verification、TaskPlan、Subgoal、ActionAttempt、Reflection、SkillMemory、MobileTaskState 与角色端口；
  - `runtime.py`：单 worker 的持久任务循环、停止、恢复、revision fence 与 no-replay；
  - `store.py`：SQLite task、plan、input、attempt、reflection、event 与 skill-memory 持久化；
  - `mobile_task_adapter.py`：Android Session、画面证据、本地模型角色 Prompt 与动作转换。
- 当前 Planner 一次产生一个包含多个有序 Subgoal 的完整 `TaskPlan`。这与新基线的 `Goal → 当前 Stage → 单 Action` 及“Planner 不输出完整动作/阶段清单”存在核心语义冲突。
- 当前任务状态、停止与恢复语义使用既有 MobileTask contract；它们不等同于新基线的 `CREATED / PLANNING / RUNNING / WAITING / PAUSED / STUCK / COMPLETED / FAILED / CANCELLED` 状态机。
- 当前 Verify 已经独立于 ADB transport：动作被接受不会自动完成 Subgoal，且新鲜 AFTER 证据参与判定。这一原则可复用。
- 当前实现有 Reflection、SkillMemory 自动作用域与晋升路径；新基线明确不建设完整 Skill 系统，也不允许首个电池任务通过专用 Skill 隐藏固定路径。是否保留这些能力、隔离为兼容层或移出新 Runtime，属于架构决策。
- 当前存储有 task event sequence 和 no-replay 恢复机制，但没有按新逻辑模型实现独立 Fact、Stage、Checkpoint 实体及其来源/验证状态。

### 3.3 Android / ADB 设备层

- `discovery.py` 通过配置的 ADB executable 做只读目标发现，区分 ready/offline/unauthorized 等状态。
- `adb_executor.py` 提供截图与结构化设备输入，使用参数数组调用 ADB；支持 tap、text/input_text、keyevent（可表达 back/home）、swipe、long-press 等原子能力。
- `mobile_task_adapter.py` 在每个动作前后获取新截图，并保存本地 PNG 证据；不把 transport accepted 当作业务成功。
- 当前 MobileTask Observation 主要是截图引用、尺寸、摘要和时间；在 MobileTask 源码路径未发现 `uiautomator dump`、UI Tree Provider 或把 foreground app、orientation、keyboard、connection state 合并为同一 Observation 的实现。
- 当前已有 DeviceExecutionLease，可防止同一进程中的 Chat、Learning 与 MobileTask 交错操作设备；这可作为新基线“单设备、单 Active Task、动作串行”的候选复用点。

### 3.4 模型与角色

- 本地模型客户端位于 `gui_owl_client.py`，通过 loopback OpenAI-compatible endpoint 访问 GUI-Owl。
- 当前 `OpenAICompatibleMobileRoleModel` 让同一 GUI-Owl endpoint 顺序承担 Planner、Executor、BEFORE 摘要、AFTER 摘要、Verifier 和 Reflection；没有新基线所定义的独立 Router / RoleBinding 选择层。
- 当前 Executor 每轮只输出一个原子动作，这是可复用行为。
- 当前模型输出会经过解析和结构校验后才转换为物理动作，可复用为 Operator Adapter 的基础。
- 云端 Chat 配置与 MobileTask 本地 GUI 角色是两条既有路径；不能直接宣称已经实现 Planner / Operator / Language 的本地/云端 Router。

### 3.5 前端

- 前端位于 `apps/console/frontend`，技术栈是 React 19、TypeScript、Vite 与 Vitest。
- 当前一级页面由 `App.tsx` 切换，主要包含“一句话开始” MobileTask、Soul、设备和设置；兼容 Chat/Learning 组件仍在源码中。
- `MobileTaskWorkspace.tsx` 通过轮询读取 Task，展示完整计划、Subgoal、Attempt、Verification 与 Reflection，并提供追加 input 与 stop。
- 当前 MobileTask UI 不是新基线的“聊天主界面 + 当前 Stage + 可展开 Phone Screen/Action/Verify/Event”；也没有 pause/resume/cancel/takeover 四种统一控制。
- 当前前端源码未使用 EventSource；任务进度依赖轮询，而不是新草案的 SSE + sequence 续传。
- 现有 React/Vite 工程、API client、状态组件、测试设施和同源静态部署可以复用。

### 3.6 持久化与运行目录

- `runtime/console/console.db`：兼容控制面数据；
- `runtime/console/mobile-tasks.db`：当前 MobileTask schema；
- `runtime/console/application-runtime.db`：通用 ApplicationRuntime；
- `runtime/console/learning.db`：Game Learning；
- `runtime/console/soul-*.db`：Soul 生命周期、集成与回复学习；
- `runtime/sessions/mobile-tasks/evidence/`：MobileTask 本地画面证据；
- `runtime/logs`、`runtime/run`：控制台与模型生命周期记录。

新基线要求 Task Snapshot、关键 Event、Stage、已验证 Fact 和 Checkpoint 持久化，但不指定数据库产品。现有 SQLite 基础设施可以复用；是否迁移当前 `mobile-tasks.db`、建立新 schema 或建立独立 store，必须在 Phase 1 前决定。

### 3.7 Soul / Hermes 边界

- 当前 Soul 通过通用 `ApplicationRuntime` 和 `applications/soul/` Adapter 接入；`F:\dating-copilot` 仍是独立的设备执行与物理 ledger owner，AI-GAME 只经 loopback owner Interface 调用。
- 当前 OpenAPI 有 Application instance、Soul scheduler 只读投影及 legacy Soul compatibility routes。
- 当前仓库未发现新文档描述的 Hermes / 微信 Gateway Client Adapter；现有 Soul owner 集成不能被当作 Hermes 契约已实现。
- 新基线把所有 Client 统一到同一个 Task Runtime；当前 Soul ApplicationRuntime、MobileTaskRuntime 与 compatibility Chat 是不同运行时边界。是否合并、适配或保留隔离属于核心模块职责决策。

## 4. 当前可运行基线

### 4.1 启动与停止

正式入口：

```powershell
cd F:\AI-GAME
.\scripts\console.ps1 setup
.\scripts\console.ps1 start
.\scripts\console.ps1 status
.\scripts\console.ps1 stop
```

双击入口：`启动控制台.cmd` / `停止控制台.cmd`。模型服务使用独立的 `scripts/model-runtime.ps1` 生命周期。

### 4.2 2026-08-10 只读运行证据

- Console：`ok`，`http://127.0.0.1:4310`，listener PID `35212`，版本 `0.1.0`；
- `GET /api/v1/health`：`status=ok`，database `ready`；
- `GET /api/v1/runtime`：overall `ready`；本地模型、ADB 与 executor 为 `ready`，云端 planner 配置只显示 `unknown`（尚未用本次检查发起真实请求）；
- GUI-Owl：`running`，`api_ready=true`，endpoint `http://127.0.0.1:4243/v1`；
- Android Target：发现一个 `ready` emulator target，具备 screen capture、touch input 与 ASCII text input 能力；
- 本次没有停止或重启进程，没有发出任何 Android 动作。

运行健康只证明服务与依赖当前可读/可连接，不证明新 Runtime 契约或电池任务验收通过。

## 5. 自动测试基线

### 后端

```powershell
cd F:\AI-GAME\apps\console\backend
..\..\..\runtime\envs\console\Scripts\python.exe -m pytest ..\tests\backend -q
```

结果：`427 passed`，耗时 `63.18s`；另有 1 条 Starlette TestClient/httpx 弃用警告。

### 前端

```powershell
cd F:\AI-GAME\apps\console\frontend
npm test -- --run
```

结果：`7 passed` test files，`39 passed` tests，耗时 `4.73s`。

上述测试证明当前旧基线内部一致，不证明新文档的状态机、API、SSE、UI Tree、Checkpoint、Hermes 或真机电池闭环已经实现。

## 6. 可复用资产

1. FastAPI + `/api/v1` 同源 Gateway 外壳、结构化错误和测试装配。
2. SQLite 事务、幂等请求、事件 sequence、投影和恢复方面的既有实现经验。
3. ADB 目标发现、连接校验、截图、通用原子动作、超时和结构化 transport result。
4. DeviceExecutionLease、revision fence、physical intent 持久化与 no-replay 恢复原则。
5. OpenAI-compatible 本地 GUI 模型客户端、结构化输出解析和单动作提案。
6. React/Vite 前端工程、API client、状态展示组件、测试和静态部署。
7. Windows 启动/停止脚本的进程身份核验与独立模型生命周期。

这些是候选复用点，不代表可以原样满足新公共契约。

## 7. 与新冻结基线的主要冲突

| 主题 | 当前实现 | 新冻结基线 | 影响 |
|---|---|---|---|
| 产品边界 | AI-GAME 是通用多应用平台，Soul 是一个 Profile | Soul Mobile Agent Workbench / 单一 Task Runtime 基线 | 必须确认新基线是替换产品边界、建立新模块还是收敛现有默认路径 |
| 任务结构 | Planner 产生完整 TaskPlan 与多个 Subgoal | `Goal → 当前 Stage → 单 Action` | 核心 Runtime 与持久化语义冲突 |
| 角色 | Planner/Executor/前后摘要/Verifier/Reflection | Planner/Operator/Language，Verify 不是新增核心角色 | RolePort 与 Router 需要重新定界 |
| 状态机 | queued/planning/running/stopping/stopped/completed/failed/uncertain 等既有语义 | CREATED/PLANNING/RUNNING/WAITING/PAUSED/STUCK/COMPLETED/FAILED/CANCELLED | 公共状态与恢复语义冲突 |
| API | inputs + stop；轮询 Task 全量状态 | messages + controls +增量 events + SSE + observations | 公共契约是破坏性变化或需要版本隔离 |
| Observation | 主要是截图与摘要 | Screenshot + UI Tree + Device State | 设备端口和证据模型不完整 |
| 前端 | 任务控制台展示完整计划/尝试；轮询 | 聊天主界面、当前 Stage、展开详情、SSE | 交互和数据投影需重新设计 |
| Skill/Memory | 有 SkillMemory 自动作用域与晋升 | MVP 不建设完整 Skill/长期记忆 | 必须隔离旧能力，避免电池任务走专用 Skill 捷径 |
| Soul | 独立 ApplicationRuntime + dating-copilot owner | Client → Gateway → 同一 Task Runtime | 模块职责不能由实现方自行合并 |

## 8. ARCHITECTURE_BLOCKER

### 发现

真实仓库不是空白工程；它已有一套生产形态的 MobileTaskRuntime、ApplicationRuntime、SQLite schema、公共 Task API 和前端工作台。新设计基线在产品边界、任务层级、状态机、角色、API、持久化与 Soul 所有权上都提出了不同的冻结定义。

### 影响

直接开始 Phase 1 会不可避免地回答以下长期问题：

- 是原地替换现有 MobileTask public contract，还是保留兼容 API；
- 是新建独立 Runtime module/schema，还是迁移当前 `mobile_agent`；
- 当前通用 AI-GAME、ApplicationRuntime、Game Learning 与 Soul owner 能力哪些继续属于产品；
- 旧 Task 数据如何读取、迁移或冻结；
- 旧前端默认入口是否保留。

这些都属于 Q1/Q2/Q10 和公共契约、模块所有权决定，超出 Phase 0 的局部实现自由。

### 候选（只提供判断材料，不代替 Owner/设计者选择）

**A. 原地迁移当前 MobileTaskRuntime**  
复用最多，但会触发 Task schema、状态、API、前端和旧数据兼容的广泛破坏性迁移。

**B. 在现有 FastAPI/前端仓库内建立独立的 v0.1 Task Runtime 模块与新 store**  
可最大限度保护旧运行能力，并逐步复用 ADB/model 基础设施；代价是暂时存在两套 Runtime，必须明确唯一入口、设备租约和兼容层边界。

**C. 把新基线作为 AI-GAME 的替代产品线重新建立最小工程**  
最贴近文档纯度，但现有大量已验证资产、数据和运行能力无法自然继承，迁移/退役成本最高。

### 已完成

- 完整阅读新设计基线；
- 工作区、代码、数据、启动、运行、API 与测试现状地图；
- 可复用点和冲突清单；
- 当前自动测试与只读运行证据。

### 未执行

- 未修改任何产品代码、API、数据库 schema 或前端行为；
- 未停止/重启服务；
- 未向 Android 下发动作；
- 未开始 Phase 1；
- 未选择 A/B/C。

### 当前施工单状态

`DONE — STOP`

下一张施工单必须先明确迁移切口、公共契约兼容策略和新旧 Runtime 所有权边界。

## 9. 证据层

- **Artifact**：本 Phase 0 现状地图已产生。
- **Tests**：当前旧基线后端 427、前端 39 项测试通过。
- **Runtime**：控制台、GUI-Owl、ADB/executor 与一个 Android target 的只读健康状态已确认。
- **Acceptance**：未执行新 MVP 电池任务；无 Owner 产品效果验收。
- **Publication/Deployment**：未部署、未发布、未提交。

## 10. RESULT

现有工程不是可直接按新路线图进入 Phase 1 的空白 Skeleton。Phase 0 已确认大量底层资产可复用，但新旧基线在核心公共语义上存在职责冲突；在设计者明确迁移切口之前继续实现会越过执行协议。

`STATUS: DONE — STOP`
