# 12 Skill 与 Memory 边界

## 1. 目的

MVP 不建设复杂 Skill 系统或长期人格记忆，但需要提前明确边界，防止任务事实、模型上下文、操作能力和长期用户信息混为一体。

## 2. 四类信息

| 类型 | 含义 | MVP 是否需要 |
|---|---|---|
| Task Fact | 当前任务中已确认的目标、约束、设备状态和结果 | 必须 |
| Checkpoint | 恢复当前长任务所需的紧凑事实快照 | 必须有结构，按阶段实现 |
| Skill | 可复用的能力说明、工具契约或操作策略 | 不建设完整系统 |
| Long-term Memory | 跨任务保留的用户偏好或长期事实 | 不在 MVP 范围 |

## 3. Task Fact

Task Fact 属于 Runtime，生命周期与 Task 相关。示例：

- 用户要求读取电池信息后回桌面；
- 当前设备标识；
- 已验证进入电池页面；
- 从页面读取到的电池字段；
- 当前 Task 的用户约束。

Task Fact 必须带来源和验证状态。它不是模型在内部形成的猜测。

## 4. Checkpoint

Checkpoint 是 Task 恢复机制，不是长期记忆。它只包含恢复 Goal、Stage 和可信进度所需的信息；Task 终结后的保存期限由部署配置决定，但不会自动升级为跨任务用户偏好。

## 5. Skill

Skill 如果未来引入，应当描述“可复用能力或领域指导”，例如某类 Observation 如何解读或某个通用工具怎样调用。Skill 不应：

- 绕过 `Observe → Decide → Act → Verify`；
- 直接把任务变成固定坐标脚本；
- 获得绕过 Gateway 或 Device Layer 的设备权限；
- 修改 Runtime 状态机；
- 把模型输出自动写成长期用户记忆。

MVP 的电池任务不能通过一个专用 `battery-page-skill` 暗藏固定导航流程，以规避“无固定点击脚本”验收。

## 6. Long-term Memory

长期记忆不在 v0.1 范围。若未来引入，至少需要单独决定：

- 哪些内容允许跨 Task 保存；
- 保存的依据和用户可见性；
- 用户如何查看、纠正和删除；
- 与设备观察、聊天内容和模型推断的区分；
- 是否允许作为新 Task 的约束输入。

这些不能由模型或 Codex 在实现 MVP 时默认决定。

## 7. 模型 Context

模型 Context 是一次或一段调用所需的临时输入，不是 Runtime 数据库。可以包含经过筛选的 Task Facts、当前 Stage、最新 Observation 和有限的失败摘要。Runtime 不保存或重放无界的完整思考过程。

## 8. 数据流规则

```text
User Message / Observation
          ↓
Runtime 解释与验证
          ↓
Task Fact / Event / Checkpoint
```

禁止的隐式数据流：

```text
Model Guess → Verified Fact
Task Fact → Long-term Memory
Skill Output → Direct ADB Command
Chat History → Checkpoint 全量复制
```

任何跨越这些边界的需求都属于新的产品和架构决定。

## 9. MVP 验收

- Task 能在不保存完整模型思考的情况下继续运行；
- 事实与推测在数据结构中可区分；
- Checkpoint 只保存恢复所需事实；
- 没有自动写入长期记忆的路径；
- 没有借 Skill 名义加入首个任务的固定点击流程。

