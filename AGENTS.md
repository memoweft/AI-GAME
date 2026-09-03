# AI-GAME 仓库规则

本仓库当前处于干净、可控的产品基线，没有自动推进的实施阶段。本文件保存长期有效边界和子 Agent 执行协议，不保存阶段流水、具体任务内容或测试计数。

## 身份与任务入口

进入本仓库的 Agent 是 bounded subagent（有界子 Agent），不是项目总代理，也不是产品所有者的另一个对话入口。只有收到总代理发出的、包含 `task_id`、任务开始时间、角色、范围、起始 commit、完成条件和停止条件的任务单后，才开始工作；信息缺失时返回阻塞项，不自行补写目标或创建阶段。

总代理会在任务单中提供必要的当前阶段上下文。子 Agent 不读取或修改 `D:\AIProjects\WeftMate\.codex\OWNER_DIALOGUE.md`、`PROJECT_STATE.md` 或 `HANDOFFS.md`；这些记录只由总代理维护。

## 先读

处理本仓库前读取：

1. `docs/product/VISION.md`；
2. `docs/product/PROJECT_MAP.md`；
3. `docs/product/EXECUTION_CONTRACT.md`；
4. `docs/product/ACCEPTANCE.md`；
5. `D:\AIProjects\WeftMate\.codex\WORKFLOW.md` 中的角色边界和交接格式；
6. 任务范围内的相关代码、测试和 `git status --short --branch`。

用户当前指令优先。

## 文档读取边界

未来 Agent 只读取本文件、根 `README.md`、`docs/product/VISION.md`、`PROJECT_MAP.md`、`EXECUTION_CONTRACT.md`、`ACCEPTANCE.md`、总代理工作流和任务单明确点名的当前材料。只有任务直接涉及开发 Console（控制台）或 GUI 模型服务时，才读取对应组件当前 `README.md`。

禁止搜索或阅读修复归档、Git 历史中的已删除文档、旧 checkout/worktree（检出目录/工作树）、`runtime/` 或 `work/` 中的说明材料、会话转储、临时日志、历史聊天摘要、rollout summary（运行摘要）和仓库本地旧 skill（技能）材料。白名单信息不足时检查当前代码、测试和运行事实，或等待产品所有者说明；不得从历史材料恢复阶段、任务、限制或实现路线。

## 产品边界

- 最终用户只在 WeftMate 中与 DeepSeek Harness（DSH，执行智能体运行时）对话。
- AI-GAME 是 DSH 调用的 Android 模拟器任务后端，不是第二个聊天、通用规划、审批或个人记忆产品。
- 只使用 Android 模拟器，不新增或恢复物理设备产品路线。
- 正式新任务只使用 `/api/execution/v2`、稳定 `{principal_id, controller_id}`、canonical Task（权威任务）与 `android_ui_agent/1`。
- `/api/execution/v1` 与非 V2 持久化字段只可为兼容读取/迁移存在，不能创建或伪装成新产品能力。
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
- 你不是仓库中唯一的工作者。只修改任务单分配的文件或职责，不回退、覆盖或整理其他 Agent 的修改；出现所有权冲突时停止并报告。
- implementer（实现 Agent）只实现并验证明确范围；explorer（探索 Agent）只读；reviewer（复核 Agent）只报告发现、不在同一任务中顺手修复；tester（测试 Agent）只执行指定验证；operator（运行准备 Agent）不代替产品所有者 dogfood。
- 新代码必须服务模拟器任务的身份、调度、观察、动作、验证、恢复、控制或有范围经验；不要把开发 Console 扩展为普通用户产品。
- 测试使用 fake/temp SQLite（临时数据库）和模拟适配器时，必须明确它只证明工程合同。
- 真实服务、模型、模拟器、WeftMate 可见性、安装包和产品所有者 dogfood（亲自试用）分别报告，不互相替代。
- 形成固定候选并完成有限内部验证后停止，等待产品所有者反馈；不自动进入无限找 Bug/修 Bug 循环。
- 任何角色都不得改变阶段、愿景、验收或任务范围，不得宣布产品所有者 `PASS`，也不得在交接后自动开始下一项工作。

## 强制交接

无论任务 `COMPLETE`、`PARTIAL` 还是 `BLOCKED`，最后回复都必须包含：

```text
task_id:
role:
status: COMPLETE | PARTIAL | BLOCKED
started_at:
completed_at:
elapsed:
waiting_time:
starting_point:
scope_owned:
work_performed:
files_changed:
commits:
verification_run:
verification_result:
runtime_or_device_evidence:
not_done:
known_issues:
integrity_blockers:
recommended_next_action:
```

计时使用实际记录；无法取得或没有记录的时间字段写 `UNRECORDED`，不得推算或编造。未执行的验证写 `NOT_RUN`，未获授权的动作写 `NOT_AUTHORIZED`。实现者自报、独立复核、运行观察和产品所有者 `PASS` 是四种不同证据，不得互相替代。
