# 05 模型角色与路由

## 1. 角色而不是模型名称

系统核心只依赖三种能力角色：

```text
Planner
Operator
Language
```

任何具体模型、本地推理服务或云端供应商都通过 Adapter 接入 Router。模型名称可以出现在运行配置、观测详情和事件元数据中，但不得成为 Task Runtime 的业务分支或公共 API 必填字段。

## 2. Planner

Planner 回答“当前阶段应该达成什么状态”。

输入：

- Goal 与 Constraints；
- 已完成 Stage；
- 已验证 Facts；
- 当前 Observation 摘要；
- 有限的失败或卡住摘要。

输出：

- 当前 Stage 描述；
- 可验证的完成条件；
- 必要的阶段约束；
- 是否需要用户补充信息。

Planner 不输出点击坐标，不维护长动作清单，不在每个设备动作后强制调用。

## 3. Operator

Operator 回答“基于当前画面，下一步怎样操作”。

输入：

- 当前 Stage 与完成条件；
- 最新 Observation；
- 有限的最近 Action 与 Verify 结果；
- 当前可用 Action 能力。

输出：

- 一个结构化 Action；
- Expected Outcome；
- 可选的短置信说明；
- 无法安全判断时的 `NO_ACTION` 或请求重新观察。

Operator 不能直接调用 ADB，不能把动作执行成功声明为 Stage 成功，也不能修改 Goal。

## 4. Language

Language 负责需要自然语言质量的工作：

- 向用户解释任务进度；
- 把已验证事实组织成最终答复；
- 根据用户要求生成内容；
- 在不改变 Runtime 事实的前提下做语言转换。

Language 不能凭空补充设备事实。对于 MVP 电池任务，反馈内容必须来源于已验证的 Observation / Fact。

## 5. Router

Router 根据角色和运行条件选择具体 Adapter。其输入可以包括：

- role；
- 所需能力，例如视觉、结构化输出、中文生成；
- 本地服务可用性；
- 延迟或成本配置；
- 当前重试与升级级别；
- 用户配置的本地/云端偏好。

Router 输出一个 Role Binding，并记录到调用元数据中。

## 6. 本地与云端

基线允许以下模式，但不固定具体实现：

```text
Planner  → 本地通用模型 → 必要时云端升级
Operator → 本地视觉操作模型 → 可配置的替代实现
Language → 本地语言模型 → 必要时云端模型
```

云端是可配置的路由目标和恢复升级机制，不是每一步都必须参与的组件。是否允许某类数据进入云端属于部署配置边界，见 [13_SECURITY_AND_USER_CONTROL_BOUNDARY](./13_SECURITY_AND_USER_CONTROL_BOUNDARY.md)。

## 7. 结构化契约

所有角色输出必须先经过 Adapter 解析和 Runtime 校验。推荐的最小结果形态：

```json
{
  "role": "operator",
  "decision": "ACTION",
  "action": {"type": "tap", "x": 824, "y": 1680},
  "expected_outcome": "进入当前条目对应的详情页",
  "confidence": 0.78
}
```

模型的自由文本不能直接变成设备命令。结构不合法、动作不在允许集合、坐标超出屏幕或缺失 Expected Outcome 时，Runtime 拒绝执行并记录失败。

## 8. 重试与升级

同一个模型反复给出相同无效动作时，Runtime 不应无限重试。恢复顺序基线为：

```text
Operator Retry
      ↓
Planner Replan
      ↓
Router Escalation（如配置允许）
      ↓
User
```

升级原因必须可见，例如连续验证失败、输出不可解析、视觉判断不确定或服务不可用。

## 9. 可观测性

展开式执行详情可以展示：角色、匿名化或配置名 Binding、调用耗时、结果类型、是否重试/升级。默认聊天界面不展示完整 Prompt、Token 流或模型冗长推理。

