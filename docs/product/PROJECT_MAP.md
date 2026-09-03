# AI-GAME 当前代码地图

> 本文描述当前活动代码边界，不是完成声明。

## 正式产品接缝

- `execution_contract/`：面向 WeftMate/DSH 的认证 API。`/api/execution/v2` 是新任务入口；`/api/execution/v1` 只读取既有记录。
- `execution_v2_composition.py`：把 V2 API 组合到 canonical Task、模拟器 Profile、frame（画面）和 experience（经验）端口。
- `agent_runtime/`：唯一顶层 Task 真相、owner-pair 隔离、revision/control、事件、时间/事件唤醒与恢复。
- `emulator_runtime/`：模拟器发现、Profile、常驻 V2 调度与生产 Android UI 组合。
- `android_ui_runtime/`：通用 `android_ui_agent/1` 的目标判据、规划、语义验证和经验接缝。
- `runtime_kernel/`：设备观察、原子动作、回执、验证、证据、租约和 crash reconciliation（崩溃对账）。
- `device_body/`：当前模拟器设备绑定和 Kernel 适配；兼容字段不是活动 transport（传输）或身份来源。
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

## 兼容代码边界

非 V2 代码和持久化字段只在当前启动迁移、只读兼容或现有测试确有调用时保留。它们不得注册为最终用户入口、创建新正式任务或决定产品路线；主线确认无依赖后，可以在单独的有界任务中删除。判断依据只能是当前调用关系、测试和运行事实，不读取已删除文档解释用途。

## 当前缺口

- WeftMate 尚未管理本服务生命周期；
- 通用 runner 的完整真实模拟器候选需要在宿主生命周期之后重新形成；
- 从一句话澄清、任务单确认到长期执行和第二轮经验复用的全链路尚未形成产品证据；
- 全局任务中心和 verified frame 不能仅凭已有组件或 HTTP 测试宣称可用；
- 当前测试/代码基础不等于 owner dogfood、安装包或发布 `PASS`。
