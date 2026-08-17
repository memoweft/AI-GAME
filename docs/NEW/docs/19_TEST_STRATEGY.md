# 19 测试策略

## 1. 测试目标

测试要证明 Runtime 的事实语义和真实设备闭环，而不仅是代码能运行。不同证据层必须分开：单元测试、组件集成、API/前端联调、模拟设备、真实 Android 和用户效果验收各自回答不同问题。

## 2. 测试层级

### 2.1 单元测试

覆盖：

- Task 状态转换；
- Event sequence 与投影；
- Stage 生命周期；
- Action Schema 校验；
- 坐标与设备状态校验；
- Verify 三态处理；
- 只有 `SUCCESS` 才 Commit；
- 用户消息分类后的状态/约束更新；
- STUCK 判定；
- Checkpoint 序列化与恢复；
- Router Binding 与非法模型输出拒绝；
- Gateway 幂等语义。

### 2.2 组件测试

使用测试 Double 验证：

- Planner 只产生 Stage；
- Operator 每次只产生一个 Action；
- Device Adapter 错误正确映射；
- Verify 机制使用动作前后 Observation；
- Language 只能访问已提供的 Facts；
- Runtime 不因模型声明直接完成 Stage。

### 2.3 API 契约测试

覆盖 [18_API_CONTRACT_DRAFT](./18_API_CONTRACT_DRAFT.md) 中的：

- Task 创建和查询；
- 消息与会话关联；
- pause/resume/cancel/takeover；
- 幂等键；
- 事件 sequence、分页和 SSE 续传；
- 统一错误模型；
- Hermes 与 Web 使用同一任务语义。

### 2.4 前端测试

- 主界面默认只展示聊天、设备、Task 和 Stage；
- 展开面板按需显示 Observation、Action、Verify 和事件；
- Runtime 状态映射正确；
- 网络重连后 Snapshot 与事件流校准；
- 暂停/继续/取消/接管调用 Gateway，而非本地伪更新；
- 旧截图显示采集时间和过期/断连状态。

### 2.5 Android Adapter 集成测试

在可控设备或模拟环境验证：

- 设备枚举和连接状态；
- Screenshot 尺寸、方向与时间戳；
- UI Tree 可用与不可用两种情况；
- tap、swipe、back、home、open_app 等通用动作；
- 断连、未授权、超时和非法坐标；
- 动作执行结果不会自行修改 Task 事实。

## 3. Verify 必测矩阵

| 场景 | Execution | After Observation | 期望 Verdict | Commit |
|---|---|---|---|---|
| 点击后进入目标页 | accepted | 明确目标页 | `SUCCESS` | 是 |
| 命令成功但页面未变 | accepted | 原页面 | `FAIL` | 否 |
| 页面加载/被遮挡 | accepted | 证据不足 | `UNCERTAIN` | 否 |
| ADB 返回失败 | rejected | 可选 | `FAIL` | 否 |
| Observation 已过期 | 未执行 | 新页面未知 | 不执行并重新 Decide | 否 |

## 4. 恢复测试

- 连续相同 Observation + Action 能触发 STUCK；
- STUCK 前不会无限重试；
- Operator Retry 使用新 Observation；
- Planner Replan 不生成完整动作脚本；
- Router Escalation 记录原因和前后 Binding；
- Pause 后无新 Action；
- Resume 后先 Observe；
- 进程重启可以从 Checkpoint + Event 恢复；
- 不确定动作不会在恢复后盲目重复。

## 5. MVP 真机端到端测试

### 前置条件

- 一台受支持且已授权的 Android 设备；
- 设备从普通桌面状态开始；
- 设置应用和电池页面未被测试代码预先定位；
- 角色 Adapter 和 Router 配置可用；
- 事件、Observation 与屏幕证据采集开启。

### 执行

发送自然语言目标：

```text
打开设置，进入电池页面，读取当前电池信息，告诉我结果，然后回到桌面。
```

测试人员不在执行过程中手动导航，除非该用例专门测试接管。

### 通过条件

- Runtime 真实创建和推进 Stage；
- Operator 的每个关键动作来自当时 Observation；
- 成功进入电池页面并保存至少一个可核对的可见 Fact；
- 最终反馈与 Fact 一致；
- Home 后新的 Observation 证明回到桌面；
- Task 最终为 `COMPLETED`；
- 没有任务专用固定脚本。

## 6. 变体测试

至少在可行范围覆盖：

- 不同设置首页布局；
- UI Tree 可用/不可用；
- 页面加载较慢；
- 屏幕方向改变；
- 第一次动作失败后恢复；
- 执行中暂停、手动改变页面、继续；
- Gateway 或 Web 短暂断线；
- Device 断开再恢复。

变体数量由实际设备条件决定，不能用单一设备通过夸大为跨 Android 版本兼容。

## 7. “无固定脚本”检查

除运行证据外，还需静态检查：

- 搜索电池任务专用函数和路径；
- 搜索固定坐标序列；
- 检查 Prompt/Skill 是否暗藏完整步骤列表；
- 检查 Runtime 是否根据 Goal 文本绕过 Operator；
- 检查测试环境是否在任务前把设备预置到目标页面。

通用 Action Schema、通用模型 Prompt 和 Android Adapter 不属于固定任务脚本。

## 8. 失败报告

失败报告至少包含：Task ID、设备和构建信息、最后状态、最后成功 Stage、最近 Observation、Action/Verify 序列、错误分类、是否可重复、是否发生人工干预。不得只报告“模型点错了”。

## 9. 完成报告

分别列出：

- 自动测试通过/失败；
- API 和前端联调结果；
- 真实设备运行结果；
- MVP 端到端验收结果；
- 用户是否确认效果；
- 尚未覆盖的环境和风险。
