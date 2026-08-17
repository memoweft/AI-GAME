# 03 Task Runtime

## 1. Runtime 的权威职责

Task Runtime 是任务事实与执行生命周期的唯一权威来源。

核心原则：

> 模型负责判断，Runtime 负责事实。

模型可以更换、失败、超时或对当前屏幕判断错误；Runtime 必须始终知道当前 Goal、当前 Stage、哪些结果已经验证、用户施加了什么约束、为什么暂停或失败，以及从哪里恢复。

## 2. 三层任务结构

Runtime 只使用三层结构：

```text
Goal
  ↓
Stage
  ↓
Action
```

- **Goal**：用户要达成的最终状态。
- **Stage**：Planner 给出的当前阶段目标，应当有可验证的完成条件。
- **Action**：Operator 基于当前 Observation 提出的单个设备操作。

Planner 不输出几十步坐标计划。Operator 不改变 Goal。Action Executor 不决定 Stage。

## 3. 执行循环

完整循环：

```text
PLAN
  ↓
OBSERVE
  ↓
DECIDE
  ↓
ACT
  ↓
VERIFY
  ↓
COMMIT
  ↓
OBSERVE ...
```

高频部分是：

```text
OBSERVE → DECIDE → ACT → VERIFY
```

Planner 只在以下情况下参与：

- Task 首次规划；
- 当前 Stage 已完成；
- 当前 Stage 失败或无法继续；
- 用户改变 Goal 或关键约束；
- Operator 被判定卡住；
- 发现当前计划没有覆盖的未知状态；
- Runtime 明确要求重新规划。

## 4. 每轮执行语义

### 4.1 PLAN

输入是 Goal、Constraints、已验证 Facts、已完成 Stage、当前 Observation 和必要的失败摘要。输出是一个当前 Stage，而不是完整动作序列。

### 4.2 OBSERVE

从设备层获取新的截图、可用的 UI Tree 和设备状态，组成带时间戳的 Observation。

### 4.3 DECIDE

Operator 获取当前 Stage、Observation、有限的最近动作与失败摘要，输出一个 Action、Expected Outcome 和必要的短说明。

### 4.4 ACT

Runtime 验证 Action 结构合法后，交给 Action Executor 执行并记录执行结果。执行成功只代表命令被设备层接受，不代表任务状态已经改变。

### 4.5 VERIFY

重新观察设备，由 Runtime 的 Verify 机制比较动作前后状态与 Expected Outcome。结果只能是 `SUCCESS`、`FAIL` 或 `UNCERTAIN`。

### 4.6 COMMIT

只有 `SUCCESS` 进入 Commit。Runtime 更新可确认 Facts、最近有效进度、Stage 进度和事件投影。`FAIL` 或 `UNCERTAIN` 不得伪装为已完成。

## 5. Runtime 输入

Runtime 接收三类输入：

1. Client 发来的用户消息与显式控制命令；
2. 模型角色的结构化输出；
3. Device Layer 返回的 Observation 和 Action Execution Result。

用户后续消息应先分类为：

- Goal Change；
- Constraint Change；
- Additional Information；
- Pause；
- Resume；
- Cancel；
- Manual Takeover。

只要能够合理关联到当前 Task，就不应无条件创建新 Task。

## 6. Runtime 输出

Runtime 对外输出：

- 当前 Task Snapshot；
- 对用户有意义的状态更新；
- 结构化 Runtime Event；
- 最终结果或明确失败原因；
- 需要用户处理的等待状态。

内部模型调用文本、供应商原始响应和冗长推理不属于稳定的对外契约。

## 7. 并发与串行原则

v0.1 面向单设备、单 Active Task。对同一设备的动作必须串行执行。来自多个 Client 的消息可以进入同一 Task，但必须由 Runtime 按接收顺序和控制语义处理，不能让两个 Client 各自驱动设备循环。

多设备并发、同设备多任务抢占和任务优先级调度不在 MVP 范围。

## 8. 进度定义

只有以下内容算作有意义进度：

- 新的 Expected Outcome 被验证成功；
- 新 Fact 被可信地确认；
- Stage 被确认完成；
- Task 因用户指令发生明确状态改变；
- Runtime 通过恢复动作进入一个新的可操作状态。

重复截图、重复模型调用、命令成功返回或时间流逝本身不算进度。
