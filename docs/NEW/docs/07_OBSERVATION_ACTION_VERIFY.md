# 07 Observation / Action / Verify

## 1. 闭环原则

系统稳定性的关键不只是会产生动作，而是把“观察”“执行”和“成功判断”分开。

> Action 执行完成 ≠ Expected Outcome 达成。

每个会改变设备状态的动作都必须处于以下闭环：

```text
Observation Before
      ↓
Operator Decision
      ↓
Action + Expected Outcome
      ↓
Execution Result
      ↓
Observation After
      ↓
Verify
      ↓
SUCCESS / FAIL / UNCERTAIN
```

## 2. Observation

Observation 是同一时间窗口内设备事实的组合：

```text
Screenshot
+ UI Tree（可用时）
+ Device State
```

每个 Observation 有唯一标识、设备标识和采集时间。组件采集时间差过大或设备在采集期间发生旋转时，应标记一致性风险，而不是假装它们是同一个瞬间。

普通 Android 页面优先组合截图与 UI Tree；游戏或自绘页面主要依赖截图。Runtime 不要求所有页面都有 UI Tree。

## 3. Action Proposal

Operator 的动作提案至少包含：

```text
action
expected_outcome
observation_id
confidence（可选）
```

`observation_id` 防止把基于旧画面的动作应用到已经变化的屏幕。若最新 Observation 与提案所依据的 Observation 不一致，Runtime 应重新决策。

Expected Outcome 应描述动作后能在设备上观察到的状态，例如“进入包含电池信息的设置页面”，而不是“点击成功”。

## 4. Verify 输入

Verify 机制获取：

- Action 前 Observation；
- Action 与 Expected Outcome；
- Execution Result；
- Action 后 Observation；
- 当前 Stage 的相关完成条件。

执行层报错时可以直接得到 `FAIL`，但执行层成功时仍必须检查新的设备事实。

## 5. Verify 结果

### 5.1 SUCCESS

有足够证据证明 Expected Outcome 达成。Runtime 可以 Commit 动作结果，并在适用时增加 Fact 或完成 Stage。

### 5.2 FAIL

有足够证据证明 Expected Outcome 没有达成，例如仍停留在原页面、进入了错误页面或出现明确错误提示。

### 5.3 UNCERTAIN

证据不足或冲突，例如截图模糊、UI Tree 与截图不一致、页面仍在加载、目标信息被遮挡。`UNCERTAIN` 不能 Commit，也不等同于 `FAIL`。

## 6. Commit

Commit 是 Runtime 的事实更新步骤，可能产生：

- `ActionVerified(SUCCESS)`；
- 新的已验证 Fact；
- Stage 进度更新；
- `StageCompleted`；
- 最近有效动作与进度时间更新；
- 必要时创建 Checkpoint。

模型输出或 ADB 返回码本身不能直接触发 `StageCompleted`。

## 7. 首个 MVP 的验证点

电池任务至少有以下可验证节点：

1. 设置应用已成为前台应用；
2. 当前页面已进入电池相关页面；
3. 页面上至少一个电池信息字段被 Observation 读取，并保存为带证据引用的 Fact；
4. 用户收到的反馈只包含已确认事实；
5. 最终前台状态为 Android 桌面；
6. Task 才能进入 `COMPLETED`。

页面名称、字段数量和文案可能因 Android 版本与设备厂商不同，验收不应把某个固定文案写死为唯一成功条件。

## 8. 避免原地循环

Runtime 用以下信号识别无进度循环：

- Observation 指纹连续相同；
- Action 类型和关键参数连续相同；
- Verify 连续 `FAIL` 或 `UNCERTAIN`；
- `last_meaningful_progress_at` 长时间不更新。

具体阈值是运行配置，不在架构中写死。超过阈值后进入恢复流程，而不是无限点击。

## 9. 可审计证据

每次 Verify 应保留足以解释判定的引用：动作前后 Observation、Expected Outcome、结果与短原因。默认界面可以只展示“已进入电池页面”，展开后能够查看对应画面和事件。
