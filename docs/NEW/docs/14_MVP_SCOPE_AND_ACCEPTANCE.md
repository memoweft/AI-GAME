# 14 MVP 范围与验收

## 1. MVP 要证明什么

v0.1 只证明一条真实的通用 Mobile Agent 闭环：

```text
User Message
   ↓
Runtime Gateway
   ↓
Goal
   ↓
Planner → Stage
   ↓
Observation
   ↓
Operator → Action
   ↓
ADB
   ↓
New Observation
   ↓
Verify → Commit
   ↓
Language Feedback
   ↓
Task Complete
```

## 2. 首个正式任务

用户用自然语言要求系统：

1. 打开 Android 设置；
2. 进入电池页面；
3. 读取当前页面可见的电池信息；
4. 把读取结果反馈给用户；
5. 返回 Android 桌面。

## 3. In Scope

- 单台 Android 设备连接与状态显示；
- ADB Screenshot、UI Tree（可用时）和 Device State；
- 最小通用 Action 集合；
- `Goal → Stage → Action` Runtime；
- Planner / Operator / Language 角色端口与 Router；
- 独立 Verify 和 Commit；
- Task Snapshot、核心事件与最小持久化；
- 基本 Retry、STUCK 检测和用户暂停/继续/取消/接管；
- Runtime Gateway；
- Web Chat 主界面和展开式执行详情；
- Hermes / 微信的统一 Client 契约与可替换适配边界；
- 首个真机任务的证据化验收。

## 4. Out of Scope

- 多设备并发；
- 同设备多任务调度；
- 任务 DAG 或通用工作流引擎；
- Agent Swarm；
- 完整 Skill 系统；
- 长期人格或用户记忆；
- 自动训练和训练数据平台；
- 复杂权限系统；
- 大量 App 专用适配；
- 完整游戏支持；
- 模型市场、Prompt 编辑器和 Token Dashboard；
- 为首个任务编写专用固定脚本；
- 把 Android 以外的环境纳入 v0.1 实现。

## 5. 功能验收标准

### A. 创建与运行

- 从 Web Client 发送自然语言后，Gateway 创建一个 Task；
- Task 有明确 Goal、状态、设备和当前 Stage；
- 同一设备上只有该 Task 的动作循环运行。

### B. 通用操作

- Planner 只给出 Stage；
- Operator 根据每轮实时 Observation 产生 Action；
- Action 通过通用 ADB 原语执行；
- 仓库中不存在该任务专用坐标序列、固定页面路径或 `open_battery_page` 式捷径。

### C. 观察与验证

- 每个关键 Action 后获取新的 Observation；
- Expected Outcome 与 Verify 结果可追溯；
- `FAIL` 和 `UNCERTAIN` 不产生虚假 Commit；
- 至少一个电池信息字段成为带证据引用的已验证 Fact。

### D. 用户反馈与收尾

- Language 输出只使用已验证 Fact；
- 用户收到可理解的电池信息反馈；
- 系统执行 Home 并验证已经回到桌面；
- 只有上述条件全部满足，Task 才进入 `COMPLETED`。

### E. 用户控制

- 运行中发送暂停后，不再产生新设备动作；
- 继续时先取得新 Observation；
- 取消进入 `CANCELLED`；
- 手动接管期间自动执行保持暂停。

### F. 可观察性

- 主聊天展示 Task 状态和当前 Stage；
- 展开详情能看到最近画面、Action、Verify 和事件；
- UI 展示与 Runtime Snapshot 一致；
- 前端关闭后 Runtime 不因界面生命周期自动停止。

## 6. 证据要求

一次合格的真机验收包至少包含：

- 用户原始指令；
- Task ID 与最终 Snapshot；
- Stage 序列；
- 关键 Observation 引用或截图；
- Action 与 Verify 事件；
- 读取到的电池 Fact 与来源；
- 发给用户的最终反馈；
- 最终桌面 Observation；
- 测试运行结果；
- 对“无任务专用固定脚本”的代码检查记录。

## 7. 通过与未通过的区别

以下单独出现都不等于 MVP 通过：

- 后端进程能启动；
- API 返回 `running=true`；
- 模型成功返回一次 JSON；
- ADB 点击命令退出码为 0；
- 单元测试全部通过；
- 在模拟截图上跑通；
- 前端展示了完成文案；
- 人工把手机提前放在电池页面。

MVP 通过需要同一次端到端真机执行中的闭环证据。

## 8. 环境差异

Android 厂商和版本会改变设置页面结构、文案和电池字段。验收目标是“基于当前 Observation 找到合理的电池信息页面并读取可见事实”，不是匹配一个预设页面标题。若设备完全不提供某类字段，系统应反馈实际可见内容，而非编造统一字段。

## 9. MVP 完成定义

代码完成、自动测试通过、服务运行、真机闭环通过和用户确认效果是不同证据层。交付报告必须分别说明，不能以其中一层替代其他层。

