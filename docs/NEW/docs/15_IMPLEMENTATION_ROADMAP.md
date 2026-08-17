# 15 实施路线图

## 1. 路线图原则

实施按可验证成果拆分。每个阶段都必须有明确范围、验证证据和 STOP 点；完成后不自动进入下一阶段。遇到新架构决定时提前停止。

路线图描述建议顺序，不授权 Codex 一次完成全部阶段。

## 2. Phase 0：现状与工程基线

**成果**：形成仓库现状地图与可运行基线。

**包含**：

- 识别现有后端、前端、ADB、模型和数据入口；
- 记录分支、HEAD、工作区状态和启动/测试方法；
- 标出与本设计冲突或可复用的部分；
- 不修改产品行为。

**验证**：现有测试/启动证据与现状地图可复核。

**STOP**：需要选择重构切口或公共接口时。

## 3. Phase 1：Runtime Skeleton + 事件事实

**成果**：能够创建 Task、保存 Snapshot、记录核心事件并执行状态转换。

**包含**：Task、Stage、Fact、Event 的最小模型；`CREATED → PLANNING → RUNNING` 基本生命周期；内存或最小持久化实现。

**不包含**：真实模型、ADB 动作、前端。

**验证**：单元测试证明合法/非法状态转换和事件 sequence。

**STOP**：成果通过后。

## 4. Phase 2：ADB Observation / Action

**成果**：通过设备层获取真实截图与状态，执行一个通用动作并返回 Transport Result。

**包含**：设备连接、Screenshot、UI Tree（可行时）、Device State、最小 Action Executor、结构化错误。

**不包含**：模型判断、任务专用导航。

**验证**：真机截图尺寸正确；通用 Home 或无业务依赖动作成功；断连错误明确。

**STOP**：成果通过后。

## 5. Phase 3：Observe / Act / Verify 闭环

**成果**：Runtime 可以执行一个由测试 Double 或最小 Adapter 提供的 Action，在新 Observation 上返回 Verify，并仅在成功时 Commit。

**验证**：覆盖 `SUCCESS / FAIL / UNCERTAIN`，证明动作执行成功不会自动完成 Stage。

**STOP**：成果通过后。

## 6. Phase 4：角色端口与 Router

**成果**：Planner、Operator、Language 使用稳定结构化契约接入；具体模型通过 Router Binding 选择。

**包含**：Adapter 接口、输出校验、超时和基础升级元数据。

**不包含**：把具体模型写死到 Runtime；复杂模型管理 UI。

**验证**：替换两个测试 Adapter 不改变 Runtime；非法输出不会触发设备动作。

**STOP**：成果通过后。

## 7. Phase 5：Runtime Gateway

**成果**：Client 能创建、查询和控制 Task，并订阅统一事件。

**包含**：`/api/v1` 最小接口、幂等、Task 关联、SSE 或等价事件流。

**验证**：API 契约测试；重复请求不重复创建 Task；断线可以按 sequence 续传。

**STOP**：成果通过后。

## 8. Phase 6：Chat Workbench

**成果**：聊天主界面可以创建并观察 Task，工程详情可展开。

**包含**：消息流、设备状态、Task/Stage、详情面板、暂停/继续/取消/接管。

**验证**：UI 状态来自 Gateway；刷新后恢复同一 Task；前端关闭不终止 Runtime。

**STOP**：成果通过后，由用户确认体验方向。

## 9. Phase 7：MVP 真机任务

**成果**：通用 Planner/Operator 在真实 Android 上完成设置电池任务。

**包含**：真实角色 Adapter、必要的通用 Prompt/Schema、证据收集、最终语言反馈和回桌面。

**禁止**：任务专用固定坐标、固定页面流程和专用 Action。

**验证**：满足 [14_MVP_SCOPE_AND_ACCEPTANCE](./14_MVP_SCOPE_AND_ACCEPTANCE.md) 的端到端证据。

**STOP**：真机闭环完成后，等待用户确认效果。

## 10. Phase 8：Hermes Client Adapter

**成果**：Hermes 使用同一 Gateway 契约创建和继续 Task。

**验证**：模拟 Client 契约测试；若真实微信条件具备，再提供真实端到端证据，两者分开报告。

**STOP**：适配契约或真实接入成果通过后。

## 11. Phase 9：长任务恢复最小化

**成果**：Checkpoint、重启恢复和 STUCK 恢复阶梯在一个受控场景中可验证。

**说明**：数据结构在前期预留，但复杂恢复实现可以在核心 MVP 闭环之后完成，除非真机任务证明它是前置条件。

**STOP**：恢复证据通过后。

## 12. 阶段变更规则

实施中允许 Codex决定局部函数、私有辅助方法、测试 Fixture 和不改变公共边界的错误处理细节。以下情况必须停止：

- 需要新增核心组件或改变职责；
- 需要改变公共 API 或数据模型语义；
- 需要修改已冻结的 Goal/Stage/Action 或 Verify/Commit 设计；
- 需要越过当前阶段的 Out of Scope；
- 存在两个以上会影响后续架构的明显方案；
- 真实环境与文档基线冲突。

