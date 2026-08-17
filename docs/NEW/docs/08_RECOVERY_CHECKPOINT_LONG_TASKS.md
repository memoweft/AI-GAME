# 08 恢复、Checkpoint 与长任务

## 1. 目标

长任务不能依靠不断追加模型 Context 来维持。Runtime 应通过任务事实、关键事件和紧凑 Checkpoint 保持连续性，并在失败后从可信状态恢复。

## 2. 失败分类

v0.1 只需要区分足以驱动恢复的失败类型：

- 模型输出不可解析；
- Action 被设备层拒绝；
- ADB 超时或设备断连；
- Verify 返回 `FAIL`；
- Verify 返回 `UNCERTAIN`；
- 连续相同画面与动作，无有意义进度；
- 当前 Stage 与真实设备状态不再一致；
- 等待用户输入或手动接管。

不需要在 MVP 建设庞大的错误分类体系。

## 3. 恢复阶梯

已经确定的恢复顺序是：

```text
Operator Retry
      ↓
Planner Replan
      ↓
Router Escalation（若配置允许）
      ↓
User
```

每一级都必须有触发原因和次数记录。恢复不是盲目重放：发生不确定执行结果或设备断连时，第一步必须重新观察。

## 4. Operator Retry

适用于短暂加载、轻微视觉不确定或一次无效动作。重试前应取得新 Observation，并把最近失败摘要提供给 Operator。

不允许对同一 Observation 无限生成相同 Action。

## 5. Planner Replan

适用于当前 Stage 无法继续、页面状态与规划假设不同、阶段目标已经部分满足或需要换路径。Planner 只重新给出当前 Stage，不把系统扩展为完整任务树。

## 6. Router Escalation

当当前模型实现持续不可用或能力不足，且配置允许时，Router 可以为相同角色选择替代实现。升级事件需要记录角色、前后 Binding 和原因。云端调用是否允许由部署配置决定。

## 7. 用户介入

以下情况可以进入 `WAITING` 或 `PAUSED` 并请求用户：

- 无法从当前证据判断下一步；
- 设备需要用户完成连接或授权；
- 用户主动要求暂停或接管；
- 恢复阶梯已经用尽；
- 需要新的产品或架构决策（工程实施阶段）。

面向用户的说明应明确当前状态、最后已确认进度和需要用户做什么。

## 8. STUCK 判定

`STUCK` 不是“运行得慢”，而是出现可观测的无意义循环：

```text
相同或等价 Observation
+ 相同或等价 Action
+ 无新的已验证 Fact / Stage 进度
```

阈值可配置，默认值应由真实测试确定。Runtime 必须保存判定依据，方便展开查看。

## 9. Checkpoint 内容

Checkpoint 至少包含：

```text
task_id
goal
constraints
status_at_checkpoint
current_stage
completed_stages
verified_facts
current_device
current_app_or_scene
last_meaningful_progress
failure_summary
created_at
last_event_sequence
```

Checkpoint 不包含完整模型思考、所有旧截图或无界的聊天历史。

## 10. Checkpoint 创建时机

- 重要 Stage 完成；
- 任务即将长时间暂停；
- 模型路由升级前；
- Runtime 计划释放旧上下文前；
- 进程准备受控停止时。

MVP 不要求每一步都创建 Checkpoint。

## 11. 恢复流程

恢复一个非终态 Task 时：

1. 读取最新有效 Checkpoint；
2. 重放 Checkpoint 之后的关键事件；
3. 验证目标设备仍是同一设备且可用；
4. 获取全新 Observation；
5. 对比已知状态与当前状态；
6. 必要时调用 Planner 重新给出 Stage；
7. 记录恢复事件后继续。

所需上下文是：

```text
Goal + Checkpoint + Current Observation
```

而不是恢复所有历史模型 Context。

## 12. 不确定动作的恢复

如果 Runtime 不知道某个动作是否实际执行，不能自动重复可能产生重复效果的动作。先观察并验证当前状态；只有确认 Expected Outcome 未发生且重试合理时，才能再次提案。

