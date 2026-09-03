# AI-GAME 仓库规则

本仓库的项目修复基线已经建立，当前没有自动推进的实施阶段。本文件只保存长期有效边界，不保存阶段流水、任务编号、代理角色、Work Order（工单）或测试计数。

## 先读

处理本仓库前读取：

1. `docs/product/VISION.md`；
2. `docs/product/PROJECT_MAP.md`；
3. `docs/product/EXECUTION_CONTRACT.md`；
4. `docs/product/ACCEPTANCE.md`；
5. 相关代码、测试和 `git status --short --branch`。

用户当前指令优先。旧 U/R/Phase、Stage 4C、归档文档和历史聊天只能用于调查，不能自动恢复施工。

## 产品边界

- 最终用户只在 WeftMate 中与 DeepSeek Harness（DSH，执行智能体运行时）对话。
- AI-GAME 是 DSH 调用的 Android 模拟器任务后端，不是第二个聊天、通用规划、审批或个人记忆产品。
- 只使用 Android 模拟器。不得恢复真机、Companion、无线 ADB、ADB reverse 或旧真机验收路线。
- 正式新任务只使用 `/api/execution/v2`、稳定 `{principal_id, controller_id}`、canonical Task（权威任务）与 `android_ui_agent/1`。
- `/api/execution/v1`、旧数据库字段和旧事件名只可为历史读取/迁移兼容存在，不能创建或伪装成新产品能力。
- 本机 Console（控制台）只供开发诊断；4310 是开发地址。正式用户不手工启动它，未来由 WeftMate 管理生命周期。

## 执行规则

- 动作前必须确认当前 owner、Task revision、Profile、设备身份和 fresh observation（新鲜观察）；动作后重新观察并验证。
- 通道接受请求、ADB 返回成功或模型声称完成都不等于用户目标完成。
- 崩溃或未知物理结果先对账，不换新 action identity（动作身份）盲发。
- 用户的暂停、修改、接管和停止优先于迟到的模型或 runner（执行器）结果。
- 普通错误进入等待、恢复或重新规划。立即阻断仅限重复真实副作用、身份/设备串线、凭据暴露和伪造成功。
- 不以固定动作数、固定迭代数或“最多 2 步”限制用户目标；用终止条件、无进展检测、退避、等待和用户控制约束循环。

## 施工纪律

- 保留用户脏工作区。未经明确授权不 reset、clean、stash、覆盖、提交、推送、发布或删除。
- 新代码必须服务模拟器任务的身份、调度、观察、动作、验证、恢复、控制或有范围经验；不要扩展旧 Console 为普通用户产品。
- 测试使用 fake/temp SQLite（临时数据库）和模拟适配器时，必须明确它只证明工程合同。
- 真实服务、模型、模拟器、WeftMate 可见性、安装包和产品所有者 dogfood（亲自试用）分别报告，不互相替代。
- 形成固定候选并完成有限内部验证后停止，等待产品所有者反馈；不自动进入无限找 Bug/修 Bug 循环。
