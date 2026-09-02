# AI-GAME 当前代码地图

> 本文描述项目修复后的活动代码边界，不是完成声明。

## 正式产品接缝

- `execution_contract/`：面向 WeftMate/DSH 的认证 API。`/api/execution/v2` 是新任务入口；`/api/execution/v1` 只保留历史读取与兼容。
- `execution_v2_composition.py`：把 V2 API 组合到 canonical Task、模拟器 Profile、frame（画面）和 experience（经验）端口。
- `agent_runtime/`：唯一顶层 Task 真相、owner-pair 隔离、revision/control、事件、时间/事件唤醒与恢复。
- `emulator_runtime/`：模拟器发现、Profile、常驻 V2 调度与生产 Android UI 组合。
- `android_ui_runtime/`：通用 `android_ui_agent/1` 的目标判据、规划、语义验证和经验接缝。
- `runtime_kernel/`：设备观察、原子动作、回执、验证、证据、租约和 crash reconciliation（崩溃对账）。
- `device_body/`：当前 ADB 设备绑定和 Kernel 适配。旧 Companion 名称只可作为持久化兼容标记，不是活动 transport（传输）或身份来源。
- `experience_runtime/`：与 owner、任务判据、Profile、设备/启动代次和应用环境绑定的执行经验。
- `user_fact_runtime/`：缺失事实的结构化状态；正式用户问题必须回到原 DSH 会话。

## 开发诊断面

- `apps/console/frontend/src/App.tsx`：执行诊断、模拟器状态和模型/执行器设置。
- `SessionWorkspace.tsx` 与 `QuestionCard.tsx`：内部 Task/问题流程诊断，不是正式聊天产品。
- `scripts/console.ps1`：开发环境 setup/build/start/status/stop/test。

Console 默认监听 `127.0.0.1:4310`。当前由开发者显式启动；正式 WeftMate 入口只有在宿主管理安装、启动、健康、停止和退出后才可发布。

## 权威数据关系

```text
authenticated {principal_id, controller_id}
  -> V2 submission alias / DSH origin provenance
  -> task_id == AgentSession.id
  -> one canonical revision/control/event history
  -> one selected emulator Profile
  -> ResidentV2TaskScheduler
  -> android_ui_agent/1
  -> RuntimeKernel observation/action/verification
```

DSH session、turn 和 tool call 只记录来源；bearer token 只认证请求；两者都不是 Task owner。不同 owner pair 的读取返回空列表或通用 not-found（未找到），不泄露对象存在性。

## 保留但非产品方向的兼容代码

旧 `/api/v1` Console、Goal、ApplicationRuntime、MobileTask、Gateway、游戏学习、旧事件名和部分数据库表仍可能被启动迁移或测试依赖。处理它们时先检查调用关系：

- 可以读取或迁移历史数据；
- 不得注册成最终用户入口；
- 不得创建新的真机/Companion 或 Settings-only 正式任务；
- 主线不再依赖时可另立有界清理任务。

旧真机应用、Companion 服务、配对脚本、相关 UI/测试和固定 Settings operator 已移出本仓库活动树，保存在项目修复归档。

## 当前缺口

- WeftMate 尚未管理本服务生命周期；
- 通用 runner 的完整真实模拟器候选需要在宿主生命周期之后重新形成；
- 全局任务中心和 verified frame 不能仅凭已有组件或 HTTP 测试宣称可用；
- 当前测试/代码基础不等于 owner dogfood、安装包或发布 `PASS`。

