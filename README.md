# AI-GAME

AI-GAME 是一个通用的本机手机智能体平台。`ApplicationRuntime` 为应用 Profile 提供持久实例、顺序 observation/policy/owner/verification 周期、revision fence、no-replay 恢复和经验门控；默认用户路径仍是“一句话开始”（MobileTask），用于提交跨多个原子 GUI 动作的长时间目标。

`F:\dating-copilot` 保持独立，并且是 Soul **唯一的设备执行与物理 ledger owner**。AI-GAME 通过 `profile_id=soul-reply-v1` 负责长期应用编排、本地视觉、云端临时回复和学习 lineage；它只通过 loopback owner Interface 请求 observation、reserve、dispatch、inspect 和 managed scheduler desired state，不复制项目代码、数据库或 ADB 控制逻辑。

Soul 只是 AI-GAME 支持的一个应用，不是产品本身。Mobile-Agent 只作为设计与实现参考；AI-GAME 没有把 Mobile-Agent 安装成父运行时，也不依赖它才能启动。

## 现在可以使用什么

- 在“一句话开始”中提交通用 Android MobileTask，查看持久计划、子目标、动作尝试、验证、Reflection 与技能版本；
- 执行中追加的指令会递增 `input_revision`；过时的模型决策不能跨过最后的设备下发 fence，但已经下发的原子动作无法撤回；
- 同一本地 GUI-Owl endpoint 顺序完成规划、单动作提案、BEFORE 单图摘要、AFTER 单图摘要、零图片的摘要对比验证和有界 Reflection，不启动常驻角色 Agent，也不在一次模型请求中传两张图；
- 连续 3 次无可见进展会持久化 Reflection 并改变策略；尺寸与 PNG 字节完全相同的 BEFORE/AFTER 画面只会在 Verifier **未确认 Subgoal 已满足**时把伪进展压成 `progress=false`，不会单独构成失败证据；若同一静态终态已经由可见事实确认满足，则保留 `satisfied=true / progress=true`；只有全部 Subgoal 被新鲜证据验证后才能完成任务并推进版本化 SkillMemory；
- 默认一句话入口会在内部推导稳定的自动技能作用域，普通用户无需填写 `skill_id`；显式旧 `skill_id` 仍兼容，但与自动作用域隔离；
- MobileTask 使用一个内部队列 worker，并在整个任务会话期间持有 `DeviceExecutionLease`；Chat、Game Learning 与 MobileTask 不会在同一进程内交错操作同一个设备；
- 正常关闭会拒绝新写入、等待已下发动作完成结算，并把未下发工作留在安全检查点；重启恢复按固定优先级处理：未收口物理 `act` 意图先终结为 `uncertain / restart_open_intent` 且永不重放，否则已接受停止的任务成为 `stopped`，其余安全活动检查点才回到 `queued`；
- 兼容 Chat API 仍可建立持久会话并读取历史消息，但 Chat 不再是默认一级入口；
- 会话空闲时发送会创建一个新的 `ChatTurn`；当前 `ChatTurn` 处于 `accepted`、`queued`、`thinking`、`planning` 或 `executing` 时继续发送，会把用户消息追加到同一个 Turn、递增 `input_revision`，并由原 worker 继续处理，不会启动第二个 worker；`stopping` 时拒绝新消息；
- 用户消息的 `delivery_status` 区分 `queued`（已持久化、模型尚未读取）、`applied`（已进入一次模型决策快照，不代表回复、动作或成功）和 `rejected`（停止、取消、失败或重启先发生，模型从未读取）；
- 选择“本地直聊”：只调用本地 GUI-Owl 生成文字，不读取屏幕、不调用云端、不操作设备；
- 选择“云端对话 + 本地执行”：云端模型生成用户回复和设备目标，本地 GUI-Owl 根据当前截图连续执行点击、长按、滑动、普通文本输入、系统返回/主页、等待和结束；
- 每一步都执行“新截图 → 本地模型提出一个动作 → ADB 传输 → 动作后新截图”，回复状态和设备执行状态分别显示；
- 当前测试模式不逐步弹审批，也不根据页面内容设置敏感类别 hard-stop；账号凭据、验证码/生物识别、实名/身份核验、付款、CAPTCHA、系统权限、法律确认或无法可靠判断的页面都不会自动触发暂停；
- 设备循环没有固定动作步数上限，只在用户主动停止、设备/ADB 或模型异常，或者本地模型返回 `terminate` 时结束；
- 若兼容旧版 GUI-Owl 的 `interact` 输出，控制台只记录一次重定向并要求模型根据新画面继续规划，不会转为人工暂停；
- 发现并展示当前 Android ADB 目标、连接类型、能力、模型/执行器状态、智能任务进度和活动事件；
- 通过 Soul Application 创建或恢复 `soul-reply-v1` 长期实例，追加语气要求、暂停、恢复或停止；dating-copilot managed scheduler 负责全天匹配和匹配后即时开场，ApplicationRuntime 负责普通回复与延迟学习；原始截图只交给本地视觉，云端只接收文字 transcript 与结构化本地视觉事实，最终发送仍由 dating-copilot 在同轮 conversation revision 下复核和执行；
- 停止正在处理的一轮对话。停止后不会再发送新动作，但已经传输到设备的原子动作无法撤回。

当前自动闭环接入 Android ADB，可绑定发现到的模拟器、USB 或无线连接的手机/平板；Windows 本机目标仍可展示，但 Windows 软件的鼠标键盘执行 Adapter 尚未接入。前端一级导航只有 **一句话开始 / Soul / 设备 / 设置**。Chat、Game Learning、Workflow、Run 和 Approval 的后端 routes 与历史记录为兼容/高级用途保留，不是一级工作台，也不会被 MobileTask worker 自动执行。当前没有一个会把自然语言自动路由成 Chat、LearningJob 或 Soul 操作的 universal Run/router。

代码、自动测试、已加载运行时和真实设备结果必须分开陈述。当前已有一条率土之滨真实任务完成记录：Task `daac81a7-1af9-47e3-9566-66e73509a0fd`，共 23 次 ActionAttempt，技能作用域 `auto:stzb/tutorial/v1`。它只证明这一条任务的完成事实，不证明通用游戏能力。新的 Soul 回复链仍待本轮 live 验收，不能据代码或测试声称已经实机跑通。

“控制台在线”“回复已生成”“ADB 已接收动作”“动作后画面已取得”“本地模型结束本轮”是不同事实，界面与数据库分别记录，不会把传输成功伪装成目标完成。

这是全部放开的测试模式，不是针对真实账号、付款、授权或法律确认流程的安全代理。请只在你明确授权和可控的测试目标中运行；页面内容本身不会替你停止循环，需要时应主动点击停止。

当前闭环是低频、顺序、单指的离散截图—动作链路，适合菜单和普通应用操作；它不具备持续视觉状态估计、高频本地控制或多点触控，尚不能宣称能够连续游玩实时动作游戏。面向训练/自定义/沙盒的未来验收边界见 `docs/gameplay-readiness.md`。

## 通用 ApplicationRuntime 平台

`ApplicationRuntime` 是应用循环的深 Module。一个 Profile 注入 `ObservationPort`、`Policy`、`ExecutionOwner`、`Verifier`、可选 `MemoryGate` 和持久化脱敏投影；调用方只管理实例，不编排截图、模型请求、物理 ledger 或恢复步骤。当前生产 Application Profile 是 `soul-reply-v1`，Soul 只是这个通用 Interface 的一个 Adapter 组合。

调用 Interface 为：

```text
start(profile_id, client_request_id, target_id=None, initial_input=None)
command(instance_id, Input|Pause|Resume|Stop, client_request_id)
inspect(instance_id)
list(limit=100)
shutdown(timeout=5)
```

本机 HTTP Adapter 对应：

- `POST /api/v1/application-instances`：创建实例，body 为 `profile_id`、`client_request_id`、可选 `target_id` 和 `initial_input`；
- `GET /api/v1/application-instances?limit=100`：读取最近实例；
- `GET /api/v1/application-instances/{instance_id}`：读取一个实例；
- `POST /api/v1/application-instances/{instance_id}/commands`：body 为 `command: Input|Pause|Resume|Stop`、`client_request_id`，且只有 `Input` 携带非空 `content`。
- `GET /api/v1/application-profiles/soul-reply-v1/scheduler`：只读投影 matcher 的目标态、有效态、控制权一致性和稳定状态码；不返回 identity、消息或 controller ref。

两个 POST 还要求同源控制台请求头 `X-AI-Game-Client: console-v1`；缺少或错误时返回 403。这个 CSRF/来源门槛不改变 `client_request_id` 的持久幂等语义。

所有写请求都使用与规范化 payload 绑定的 `client_request_id`：同 ID、同 payload 返回同一个持久结果；同 ID 改 payload 冲突。HTTP 只投影 lifecycle、revision、degraded/hard-risk、脱敏 intent phase、Outcome 状态和时间，不返回输入正文、对方消息、回复草稿、截图、owner receipt 细节或验证证据正文。

## 通用 MobileTask 运行时

当本地 GUI-Owl、已启用的 Android executor 和 ADB executable 已配置时，后端组装一个通用 `MobileTaskRuntime`；默认界面要求选择就绪 Target，并按其动态 serial 绑定，所以不要求全局默认 serial。兼容 API 若省略 Target 才尝试默认 serial；默认值也不存在时，请求可先持久化为 `202 queued`，随后以 `executor_not_configured` 收口。缺少整个运行时依赖时，控制台与持久历史仍可打开：`GET /api/v1/tasks` 和 `GET /api/v1/tasks/{task_id}` 通过只读 `MobileTaskArchive` 查询 `mobile-tasks.db`，而创建、追加输入和停止三个写 routes 返回 `503 mobile_task_runtime_not_configured`，不伪造已经排队或正在执行的状态。

调用 Interface 只需要五类操作：

- `POST /api/v1/tasks`：提交目标；
- `GET /api/v1/tasks`：列出最近任务；
- `GET /api/v1/tasks/{task_id}`：查看完整 TaskState；
- `POST /api/v1/tasks/{task_id}/inputs`：追加 owner 指令；
- `POST /api/v1/tasks/{task_id}/stop`：请求停止。

元数据位于 `runtime/console/mobile-tasks.db`（独立 schema v2），本地原始画面证据位于 `runtime/sessions/mobile-tasks/evidence/`。证据目录在每次新增画面后尽力清理不完整文件和旧记录，默认按 256 帧、1 GiB、7 天修剪，同时始终保留刚写入的一对文件，因此这些是 best-effort retention 而不是对最新证据的绝对总量上限；SQLite 任务历史可以比原始帧保留更久。HTTP 中的 ActionAttempt 不返回原子动作参数、必须原样输入的文本、原始模型输出或 BEFORE/AFTER 截图引用；事件也只返回序号、类型和时间。传给下一次 Executor 的近期动作历史只使用脱敏指纹：点击/长按只保留 4×4 屏幕区域，滑动只保留方向，文本固定为 `text(redacted)`。用户自己提交的 goal 和 input 仍是任务 Interface 的一部分，会在查询时显示。

完整的 Module、Interface、seam、Adapter、并发、验证、SkillMemory 和恢复规则见 [`docs/mobile-task-runtime.md`](docs/mobile-task-runtime.md)；精确 HTTP 请求、响应、错误和脱敏契约见 [`contracts/mobile-task-v1.md`](contracts/mobile-task-v1.md)。

## 兼容的实验性有界游戏学习

首版 `GameLearner` 为低频离散 GUI 提供有界的 `LearningJob` / `LearningEpisode`：它记录追加式 `Transition` ledger，把画面证据保存为本机文件，由独立 `OutcomeVerifier` 判断后置条件，再生成 `RewardSignal`，并在证据门槛满足时把合格轨迹蒸馏成版本化 `PolicyMemory`。这条链路只做轨迹蒸馏，不训练或微调模型权重。

调用界面保持为一句话：`start` / `shutdown` 管生命周期，`list_profiles`、`learn`、`list_jobs`、`inspect`、`stop` 管有界学习工作；调用方不直接编排截图、验证、奖励、蒸馏或 PolicyMemory 晋升。对应的本机 HTTP routes 是：

- `GET /api/v1/learning/profiles`；
- `POST /api/v1/learning/jobs`；
- `GET /api/v1/learning/jobs`；
- `GET /api/v1/learning/jobs/{job_id}`；
- `POST /api/v1/learning/jobs/{job_id}/stop`。

首个 `stzb-tutorial-v1` Profile 只允许固定、已授权、已登录环境中的教程推进和只读菜单导航；它禁止登录/账号、验证码、实名、支付/充值、领取/招募/强化、聊天/联盟、出征、真人交互和公开竞争等目标。这个名称是一个窄 Profile，不代表 AI-GAME 已适配、已认证或“已经会玩率土之滨”。

学习元数据使用独立的 `runtime/console/learning.db`，证据位于 `runtime/sessions/game-learning/`。ADB 接受动作不等于 Outcome 已确认。首版不持久化候选：只有 Episode 被证据确认成功且含至少一个正奖励物理 Transition 时，确定性轨迹蒸馏才会在同一事务中插入并自动晋升不可变 PolicyMemory 新版本；否则保持 `unchanged` / `not_learned`。`candidate`、`rejected`、`distilling`、`validating` 只为未来显式候选流程保留，未来候选也不得因创建或重启而自动激活。停止或重启不会重放物理动作，不确定的停止必须保留为 `stopped_uncertain`，不得自动补发。

完整设计与 HTTP 语义见 [`docs/game-learning.md`](docs/game-learning.md) 和 [`contracts/game-learning-v1.md`](contracts/game-learning-v1.md)。

## 启动控制台

最简单的方式是双击根目录下的 `启动控制台.cmd`。控制台准备好后会自动打开：

```text
http://127.0.0.1:4310
```

第一次使用，或者依赖尚未安装时，在 PowerShell 中运行：

```powershell
cd F:\AI-GAME
.\scripts\console.ps1 setup
.\scripts\console.ps1 start
```

常用命令：

```powershell
.\scripts\console.ps1 status    # 查看控制台是否已启动
.\scripts\console.ps1 stop      # 停止控制台，不影响其他程序
.\scripts\console.ps1 test      # 运行后端、前端测试并重新打包
```

也可以双击 `停止控制台.cmd` 安全停止。对于由 launcher 启动的实例，停止脚本会用每次启动随机生成的本机 token 请求 Uvicorn 优雅退出，并等待运行时收口；只有优雅退出超时或旧版 state 不支持该协议时，才会在核对进程身份后使用强制回退。它不会在端口被其他程序占用时误杀其他程序。

本地直聊和设备执行需要 GUI-Owl 服务。查看或启动它：

```powershell
cd F:\AI-GAME
.\scripts\model-runtime.ps1 status
.\scripts\model-runtime.ps1 start
```

控制台本身不会擅自启动或停止模型服务；两者有独立的生命周期。

本地 GUI-Owl 的机器配置位于 `config\model-runtime.env`；首次配置时复制
`config\model-runtime.env.example`，再按本机 WSL 路径和显存条件调整。真实
`model-runtime.env` 不应进入版本控制。其 `GUI_MODEL_API_KEY` 仅是回环 GUI-Owl
服务使用的本地 bearer token，也会被控制台读取为本地模型 key；不要在该文件中
写入任何云端凭据。云端 API key 仍只应通过下文的进程环境变量或控制台 DPAPI
设置保存。

## 配置 Android ADB 执行器

执行 Adapter 使用已配置的 ADB executable 组装运行时；默认 serial 是可选兼容 fallback，不是组装 MobileTask 的前提。只读发现执行 `adb devices -l`，会把就绪 Android 目标标记为 `emulator`、`usb` 或 `wireless`，并报告 `screen_capture`、`touch_input`、`ascii_text_input` 等能力。MobileTask 显式选择某个就绪 Android Target 时，会在同一 ADB executable 上为该 Target 动态绑定 serial；默认 serial 只在没有显式选择 Target 时使用。当前 TaskSession 绑定后不会因发现结果变化静默切换设备；恢复会用持久化 `target_id` 重新解析并打开新的 Session。

AI-GAME 不会替用户启动模拟器、打开 USB 调试、批准设备授权或建立无线配对。真实手机/平板必须先由用户在操作系统和 ADB 中完成授权并出现在 `adb devices -l`；“已发现/transport ready”也不等于任务或应用结果已经通过验证。

### 可选的 MuMu 配置助手

MuMu 的 ADB 端口可能随本机实例变化。仅当 MuMu VM 0 已由你自己启动后，运行：

```powershell
.\scripts\sync-mumu-executor.ps1
```

该脚本只读取 `mumu-cli info --vmindex 0`，拒绝未启动实例和非回环地址；随后使用 MuMu 自带的 `adb.exe` 连接并验证 `device`，再原子更新非秘密配置 `config\executor-runtime.env`（当前 `enabled=1`）。它绝不会自动启动、创建、控制或删除 MuMu 实例。

控制台启动时会安全地读取这个配置的三项白名单环境变量：
`AI_GAME_GUI_EXECUTOR_ENABLED`、`AI_GAME_ADB_PATH`、`AI_GAME_ADB_SERIAL`；已在启动命令中显式设置的同名环境变量优先。可重复检查：

```powershell
.\scripts\test-executor-runtime-config.ps1          # 静态配置与优先级检查，不接触 MuMu
.\scripts\test-executor-runtime-config.ps1 -Live    # 再执行一次只读发现和 ADB 连通性验证
```

“执行器配置已验证”仅表示当前动态 ADB 目标可连接，不是“任务已执行”或“任务已完成”。每次原子动作前执行器都会重新核对设备状态；端口变化后不会静默改投另一个设备，需要重新运行同步脚本并重新发现目标。

## 兼容 Chat 的云端对话模型

云端模式使用独立的 OpenAI-compatible 配置，不读取其他项目或应用的密钥。

1. 打开控制台左侧“设置”，在“云端模型配置”中填写服务地址、模型名称和独立 API key；
2. 点击“保存配置”。保存后立即用于之后新建的 Turn，无需停止或重启控制台；已经运行的 Turn（包括执行中追加的消息）继续使用它开始时捕获的模型连接；
3. 如需验证兼容性，点击“测试连接”。这是一次真实的模型请求，可能产生服务方计费。

API key 由后端使用 Windows DPAPI 按当前 Windows 用户保护后保存；页面和设置接口只显示“是否已有密钥”，不会回显密钥。端点、模型、受保护的密钥数据与配置修订保存在本机控制台数据库中，不写入消息、事件或日志。DPAPI 保护意味着保存的密钥跟随当前 Windows 用户边界；无法解密时控制台会要求重新保存，不会退回明文存储。

`config\cloud-runtime.env` 和进程环境变量只用于没有控制台保存记录时的首次启动引导或自动化部署。环境文件只能放非秘密的端点和模型：

   ```text
   CLOUD_CHAT_ENDPOINT=https://你的服务地址/v1
   CLOUD_CHAT_MODEL=你的模型名
   ```

如需使用启动引导密钥，只在启动控制台的同一个 PowerShell 进程中设置占位值，绝不要把密钥写进 `cloud-runtime.env`：

   ```powershell
   $env:AI_GAME_CLOUD_CHAT_API_KEY = '你的独立 API key'
   .\scripts\console.ps1 start
   ```

一旦通过控制台保存或清除配置，本机保存的配置修订在后续启动时优先于启动引导；保存、测试和清除都会热更新运行状态，不需要重启。正在处理的请求不会被追溯改写，后续请求使用最新已保存配置。云端模式会发送本会话的文字内容；原始设备截图不会发送给云端，只会发给 `127.0.0.1` 上的本地 GUI-Owl 服务。未完成配置时，控制台仍可使用本地直聊，并会明确把云端模式显示为未配置。

## 目录规范

```text
F:\AI-GAME
├─ apps\console\
│  ├─ backend\                 Windows 本机控制面 API 与 SQLite 持久化
│  └─ frontend\                中文浏览器控制台
├─ config\                     非敏感配置示例与运行配置
├─ contracts\                  控制面接口和数据边界
├─ docs\                       架构、运行目录与操作说明
├─ scripts\                    控制台和模型服务的生命周期脚本
├─ services\gui-model\         预留的本地 GUI 模型服务集成
├─ workflows\                  后续按应用/模拟器拆分的工作流
└─ runtime\                    自动生成的数据，全部忽略版本控制
   ├─ console\                 本机 SQLite 状态
   │  ├─ console.db            控制面、对话与云端配置
   │  ├─ learning.db           独立的游戏学习 ledger 与 PolicyMemory 元数据
   │  ├─ mobile-tasks.db       MobileTask 状态、意图、验证与 SkillMemory
   │  ├─ application-runtime.db 通用 ApplicationInstance 周期、意图和 Outcome
   │  ├─ soul-scheduler-lifecycle.db Soul matcher 持久目标与单调控制代次
   │  ├─ soul-reply-learning.db Soul 草稿 lineage 与延迟互动结果
   │  └─ soul-integration.db   旧 SoulIntegration 兼容数据，不是当前写路径
   ├─ envs\console\            控制台独立 Python 环境
   ├─ logs\                    控制台与服务日志
   ├─ run\                     PID 等实时状态
   ├─ models\                  后续下载的模型快照
   ├─ sessions\                每次运行的轨迹与证据
   │  ├─ game-learning\        LearningEpisode 的本机证据与派生物
   │  └─ mobile-tasks\evidence\ MobileTask 的本机 PNG/尺寸证据
   └─ screenshots\             按保留策略保存的截图
```

控制台生产模式由后端在同一个本机地址提供前端页面，不需要同时维护两个端口。数据库、环境、日志和打包产物都与源代码分开保存。

## 执行边界

默认“一句话开始”的 MobileTask 执行链为：

```text
owner goal / 追加指令 + 自动或显式作用域中的上一版 SkillMemory
→ 本地 GUI-Owl Planner 生成版本化 TaskPlan / Subgoal
→ 获取新鲜 BEFORE 画面
→ 同一个本地 GUI-Owl Executor 提出一个结构化动作
→ 先持久化 ActionAttempt 与物理意图
→ 最后一次 stop / input_revision fence
→ Android ADB Adapter 最多下发一个原子动作
→ 接收物理动作后等待 1 秒 settle（显式 wait 使用请求的 0–10 秒）
→ 获取新鲜 AFTER 画面
→ BEFORE 摘要角色只读一张 BEFORE 图并产出可见事实
→ AFTER 摘要角色只读一张 AFTER 图并产出可见事实与遮挡信号
→ Verifier 不读取图片，只比较两份文字摘要 + 本机同帧判定
→ 推进 Subgoal，或在连续 3 次无进展后由 Reflection 改变策略
```

MobileTask 不调用云端聊天模型：Planner、Executor、BEFORE/AFTER 摘要、Verifier 和 Reflection 共用同一个本地 GUI-Owl endpoint，并按角色顺序调用，不是多个并行进程。最终 Verifier 只收到两份有界可见事实、AFTER 遮挡信号和本机同帧判定；任何单次模型请求都不会同时收到 BEFORE 与 AFTER。固定动作解析器只接受受支持的 `mobile_use` 动作和归一化坐标，ADB 始终使用参数数组并关闭 shell。1 秒 settle 只给界面留出刷新时间，不代表应用已经稳定或目标成功。`accepted` transport 也只说明设备通道接受了输入；只有 Verifier 基于新鲜画面把当前 Subgoal 判定为 `satisfied` 才能推进，全部 Subgoal 都满足后任务才会 `completed`。生产配置把 2,048 次 ActionAttempt 和 64 次 Reflection 作为长任务的 runaway guard；每逢连续 3 次无进展仍先 Reflection，而不是盲目重复。

追加指令、停止和运行时关闭都与物理下发 seam 共享最后的串行 fence。它们先到达时，旧意图会以 `not_sent` 收口；动作已经进入下发 seam 后无法撤回。运行时会先保存真实 transport、AFTER 与 Verification 结算事实：新 revision 或停止会阻止旧结果推进，shutdown 则允许已经下发的这一个动作正常结算，随后不再开始新动作。正常 shutdown 把下一安全检查点重新置为 `queued`，并由下一进程恢复；异常重启发现未收口物理 `act` 意图时，任务进入 `uncertain` 并且不重放。

“云端对话 + 本地执行”是独立的 Chat Module：云端模型只能生成文字回复和高层目标，不能生成或发送 ADB 命令，也看不到原始截图。本地 GUI 模型一次只提出一个动作；生产 Chat 循环没有固定步骤上限，也不运行页面敏感类别分类器。它会持续到用户停止、设备/模型异常或本地模型返回 `terminate`。Provider 回复、GUI 动作提案和 `terminate` 都绑定到产生它们的 `input_revision`；若期间收到更新，旧输出会被丢弃，同一个 worker 根据最新消息重新规划。更新若在最后一次动作发送前检查之后才到达，只能影响下一次决策，不能撤回已经交给 ADB 的原子输入。Chat 的模型结束信号不是应用内部业务状态证明。

相关目录规则见 [`docs/runtime-layout.md`](docs/runtime-layout.md)，MobileTask 设计与精确契约见 [`docs/mobile-task-runtime.md`](docs/mobile-task-runtime.md) 和 [`contracts/mobile-task-v1.md`](contracts/mobile-task-v1.md)，其余控制面 HTTP 与状态契约见 [`contracts/control-plane-v1.md`](contracts/control-plane-v1.md)，有界学习契约见 [`contracts/game-learning-v1.md`](contracts/game-learning-v1.md)。
实时游戏控制尚未实现；训练/自定义/沙盒限定的目标架构、遥测要求和未来验收线见 `docs/gameplay-readiness.md`。

## Soul Application Profile

Soul 工作台使用通用 `/api/v1/application-instances` Interface；启动时固定提交 `profile_id=soul-reply-v1`，之后用 `Input`、`Pause`、`Resume`、`Stop` 管理同一个长期实例。浏览器不直接跨端口调用 Soul，也不会读取 dating-copilot 的 SQLite、导入它的 Python 包或接触 ADB。

每个回复周期为：

```text
dating-copilot owner 捕获一条到期 pending inbound、稳定 transcript、revision 与 PNG
→ AI-GAME 本地视觉只消费 PNG，并立即丢弃图片正文
→ 云端模型只接收 transcript、结构化本地视觉事实、当前策略与 owner 指令
→ ApplicationRuntime 持久化脱敏 intent，并执行最后 revision fence
→ dating-copilot reserve 物理意图
→ dating-copilot 在同一个 conversation_revision 上再次 preflight 后 dispatch
→ inspect owner 物理 ledger，确认、等待 reconciliation 或 terminal no-replay
```

AI-GAME 使用的 owner v1 loopback Interface 是：

- `GET /api/application-owner/v1/capabilities`；
- `POST /api/application-owner/v1/soul/observations`，body 为 `{"contract_version":"v1"}`；
- `POST /api/application-owner/v1/soul/intents`，body 含 `application_intent_id`、`scope_ref` 和单条 `draft.text`；
- `POST /api/application-owner/v1/soul/intents/{owner_ref}/dispatch`，再次携带 `scope_ref`、`preflight.conversation_revision` 和同一 `draft.text`，供 owner 校验不可变 hash；
- `GET /api/application-owner/v1/soul/intents/{owner_ref}`；恢复 reserve 绑定时可按 `GET /api/application-owner/v1/soul/application-intents/{application_intent_id}` 查找 owner ref。
- `GET /api/application-owner/v1/soul/scheduler` 与 `PUT /api/application-owner/v1/soul/scheduler`；PUT 只接受 `contract_version=v1`、`desired_state=running|paused|stopped` 和固定 `controller_ref=ai-game-soul-reply-v1`。

reserve 与 dispatch 分开是物理 commit fence，不是让 AI-GAME 接管设备。`dating-copilot` 仍是唯一设备与物理 ledger owner；owner 返回 `uncertain_needs_reconciliation` 时，ApplicationRuntime 只 inspect/reconcile，不重新 dispatch。`active_dispatch` 也只进入可中断等待并重复 owner GET，不 reserve、不 dispatch、也不生成替代草稿。进程恢复发现未收口 intent 时同样先走 inspect-only reconciliation；找不到可信证明就以 `recovery_no_replay` 收口。

managed scheduler 只保留匹配职责：全天使用机会，空闲时段提高 cadence，成功匹配后立即开场，并且只有可信的当日配额为零时才进入 Planet；它不生成或发送普通回复。所有 Soul instance 共同聚合成一个 matcher 目标：任一 nonterminal instance 处于 `queued`、`running`、`waiting`，或其 `stopping` 前目标为 running，就优先选择 `running`；否则只要有 `paused` 或停止前目标为 paused，就选择 `paused`。Input 不改 matcher，`stopping` 保留该 instance 的 Stop 前目标，只有 core 真正结算为 `stopped` 的 Stop 才能贡献 stopped。

`soul-scheduler-lifecycle.db` 持久化不含正文的 singleton receipt：`requested_state`、实际可下发的 `desired_state`、source instance、hash transition ref、单调 generation 和时间。存在 nonterminal instance 时按上述需求聚合；不存在时采用最新的显式 lifecycle 证据，但未结算的 Stop 不算 stopped，`failed`/`completed` 保持其最近显式目标，不能让更早的 Stop 重新生效。单个可中断 monitor 每轮先 GET、仅在不一致时 PUT；dating-copilot 反向重启或暂时不可达时按持久目标收敛，cloud/local vision 不可用也不会拖停 matcher。冷恢复且回复依赖不可用时，只委托 Application core 结算真正 idle 的 Stop；worker token 或未完成 intent 不会被清理。AI-GAME shutdown 会关闭本地 activation、monitor 和已加载的 Application runtime；它在 owner GET 后、PUT 前再次检查关闭栅栏，绝不隐式 PUT `stopped`，也不会让阻塞 probe 后的 factory 或晚返回的 candidate 重新发布 runtime。

冷启动恢复 paused 实例时允许 owner 报告 `desired_state=paused / effective_state=stopped`，无需先短暂启动 matcher。浏览器只读 `GET /api/v1/application-profiles/soul-reply-v1/scheduler`，所有写控制仍统一走 ApplicationRuntime Start/Pause/Resume/Stop，不存在第二套 scheduler 按钮。

若出现不受 managed contract 控制的旧 worker/模式，owner 的 `legacy_scheduler_active` 仍是拒绝混跑，不是回退指令。它和 direct `soul_execution_runtime_unavailable` 都是 definite-not-sent：ApplicationRuntime 随后只 GET/inspect 到 `terminal_no_replay`，把本次尝试结算为 nonterminal `confirmed_failure` 后重新观察规划，不重放旧 intent 或草稿。AI-GAME 不导入或直接调用 dating-copilot 的 Python 调度函数，也不通过旧 Soul command route 启动它。

Observation 没有 DELETE/abandon route。reserve 之前，下一次 fresh observe 只能原子替换 process-local 的 unreserved scope；旧 scope 随后返回 `observation_scope_stale`。reserved/active pipeline 不会被替换，`foreground_action_owned` 进入等待重试，绝不授权旧草稿下发。

一次已确认发送只证明送达，不是回复策略奖励。`soul-reply-learning.db` 记录 transcript/pending generation、草稿 hash、策略、prompt/persona/model 版本、owner ref 和 send proof；只有之后出现新的对方 inbound，或带完整时间与“确无新 inbound”证明的延迟结果，才更新 reply strategy。ApplicationRuntime 的 `SoulReplyMemoryGate` 不会把当前发送成功直接晋升为经验。reserve 前创建 learning trial 失败时尚无 owner material，可重试；reserve 或送达已经由 owner 持久化后，本地 bind/send-proof 写入失败只能留下待修复 lineage，不能改写 owner 的送达事实、阻止唯一一次 fenced dispatch，或触发第二次 reserve/send。

旧 `GET /api/v1/integrations/soul` 与 `GET /api/v1/integrations/soul/conversations/{conversation_id}` 仅保留兼容只读诊断。旧 `POST /api/v1/integrations/soul/commands` 和 `POST /api/v1/soul/commands` 固定返回 `410 legacy_soul_write_disabled`，不得再作为主运行逻辑。

默认配置为：

```text
AI_GAME_SOUL_CONSOLE_URL=http://127.0.0.1:5000
AI_GAME_SOUL_TIMEOUT_SECONDS=5
AI_GAME_SOUL_OBSERVATION_TIMEOUT_SECONDS=90
```

`AI_GAME_SOUL_TIMEOUT_SECONDS` 只约束 owner 的 capabilities、scheduler、reserve、dispatch 与 inspect 等短控制请求；`AI_GAME_SOUL_OBSERVATION_TIMEOUT_SECONDS` 单独约束 owner 捕获一帧可用观察的等待时间。端点只接受 HTTP loopback 地址。AI-GAME 的启动、停止和模型脚本不会启动、终止或修改 `F:\dating-copilot` 的进程或文件；只有显式 Application lifecycle 命令会通过上述 owner API 改变 managed scheduler desired state。
