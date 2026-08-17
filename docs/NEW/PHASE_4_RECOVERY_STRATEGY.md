# Phase 4 恢复策略设计

**状态**: DESIGN DRAFT  
**日期**: 2026-08-17  
**依据**: `PHASE_4_RECOVERY_VERIFICATION.md` 验证结果

---

## 1. 设计决策：UNCERTAIN 自动 Checkpoint

### 1.1 当前行为

```python
verification, checkpoint = kernel.verify_action(
    verdict=VerificationVerdict.UNCERTAIN,
    ...
)
# checkpoint = None (不自动创建)
# 需要手动调用：
kernel.create_checkpoint(
    task_id=task_id,
    unresolved_action_ref=action_id,
    reason="uncertain physical outcome"
)
```

### 1.2 问题分析

**UNCERTAIN 的语义**：
- 物理动作的执行结果无法确定（例如：tap 发出了，但屏幕状态不符合任何预期）
- 不是验证逻辑失败（那是 FAIL）
- 不是成功（那是 SUCCESS）

**为什么需要 Checkpoint**：
- UNCERTAIN 后继续执行新 Action 可能基于错误的状态假设
- 必须要求人工检查或新 Observation 才能安全继续
- `unresolved_action_ref` 是阻止盲目重试的语义标记

**当前设计的问题**：
- 外层调用者必须记得在 UNCERTAIN 后手动创建 Checkpoint
- 容易遗漏，导致 UNCERTAIN 状态下继续执行（不安全）

### 1.3 推荐方案：自动创建 Checkpoint

**修改 `verify_action()`**：

```python
if verdict is VerificationVerdict.UNCERTAIN:
    # 记录 Verification，更新 Task failure_state
    self._store.record_verification(...)
    
    # 自动创建 Checkpoint
    checkpoint = self._build_checkpoint_draft(
        task=after_task,
        stages=...,
        verified_facts=self._store.list_facts(task_id, verified_only=True),
        reason="verification_uncertain",
        checkpoint_id=checkpoint_id or self._id_factory(),
        unresolved_action_ref=action_id,  # 关键：标记未解决的 Action
    )
    after_task = after_task.record_checkpoint(checkpoint.id, at=now)
    materialized_checkpoint, _ = self._store.create_checkpoint(...)
    return verification, materialized_checkpoint
```

**效果**：
- `verify_action(verdict=UNCERTAIN)` 总是返回 `(Verification, Checkpoint)`
- `Checkpoint.unresolved_action_ref` 指向 UNCERTAIN 的 Action
- `Checkpoint.required_fresh_observation = True`
- 后续 `prepare_action_execution()` 自动拒绝基于旧 Observation 的决策

**向后兼容**：
- 当前测试期望 `checkpoint = None`，需要更新
- 生产代码无影响（还未集成）

### 1.4 替代方案：保持手动

**理由**：
- UNCERTAIN 可能是暂时性的判断失败（例如：验证规则太严格）
- 不应该强制阻塞后续工作
- 外层有足够上下文决定是否需要 Checkpoint

**反驳**：
- UNCERTAIN 本质是"物理结果未知"，不是"验证逻辑失败"
- 在结果未知时继续执行是不安全的
- 如果验证逻辑太严格，应该改进验证规则，而不是忽略 UNCERTAIN

**结论**：**推荐自动创建**，更符合 UNCERTAIN 的语义。

---

## 2. 人工恢复策略

### 2.1 恢复触发条件

当加载 Task 时检测到：
```python
checkpoint = kernel.latest_checkpoint(task_id)
if checkpoint and checkpoint.unresolved_action_ref:
    # 需要人工恢复决策
```

### 2.2 恢复决策流程

#### Step 1: 呈现上下文

**UI 展示**：
```
⚠️ Task "打开设置页面" 需要人工恢复

未解决的 Action:
  ID: action-abc-123
  类型: TAP
  参数: {"x": 540, "y": 1200}
  预期结果: "设置页面可见"
  
最后状态:
  Verification: UNCERTAIN
  原因: "点击后屏幕未出现预期的设置页面标题"
  
最后 Observation:
  时间: 2026-08-17 10:15:32
  前台应用: com.android.settings
  屏幕截图: [显示]
  UI 树: [显示]
```

#### Step 2: 人工检查

**操作员检查清单**：
1. 查看最后 Observation 的截图和 UI 树
2. 查看物理设备当前屏幕（可能已经变化）
3. 对比 Action 的预期结果
4. 判断物理动作是否实际执行

#### Step 3: 决策选项

**选项 A: 补偿（Mark as Success）**
- **适用**：检查后发现 Action 实际成功了，验证规则太严格
- **操作**：
  ```python
  kernel.verify_action(
      action_id=action_id,
      verdict=VerificationVerdict.SUCCESS,
      reason="Manual verification: action succeeded",
      verified_facts=[...],  # 手动添加 Facts
      complete_stage=True/False,
  )
  ```
- **效果**：写入 Facts，可能完成 Stage，创建新 Checkpoint（无 `unresolved_action_ref`）

**选项 B: 重试（New Action）**
- **适用**：物理动作失败或状态不明，需要重新观测和决策
- **操作**：
  ```python
  # 1. 标记旧 Action 为 FAILED
  kernel.verify_action(
      action_id=action_id,
      verdict=VerificationVerdict.FAIL,
      reason="Manual review: physical action did not succeed",
  )
  # 2. 捕获新 Observation
  obs = kernel.capture_observation(task_id, device_id)
  # 3. 提出新 Action
  new_action = kernel.propose_action(task_id, stage_id, ...)
  ```
- **效果**：旧 Action 归档，新 Action 基于最新 Observation

**选项 C: 跳过（Mark as Failed）**
- **适用**：物理动作失败，但决定不重试，继续其他路径
- **操作**：
  ```python
  kernel.verify_action(
      action_id=action_id,
      verdict=VerificationVerdict.FAIL,
      reason="Manual review: skip this action path",
  )
  # 然后 propose 新 Action 尝试不同路径
  ```
- **效果**：Action 归档为 FAILED，Stage 保持 ACTIVE，可以尝试其他方案

**选项 D: 终止 Task**
- **适用**：无法恢复，Goal 无法达成
- **操作**：
  ```python
  kernel.close_task(task_id, reason="Manual termination: unrecoverable")
  ```
- **效果**：Task 终止，释放资源

### 2.3 自动化可能性

**幂等 Action 的自动补偿**：
- 对于幂等操作（例如：tap 同一个按钮），可以自动重试
- 前提：需要 Action 类型元数据标记幂等性
- Phase 4 不包含，Phase 5+ 考虑

**基于规则的自动决策**：
- 例如：UNCERTAIN 超过 3 次 → 自动终止
- 或：特定 error_code → 自动重试
- 需要更多生产数据支撑规则设计

**当前建议**：**全部人工决策**，积累经验后再自动化。

---

## 3. Checkpoint 恢复 API 设计

### 3.1 查询恢复点

```python
# GET /runtime/tasks/{task_id}/recovery
{
    "needs_recovery": true,
    "checkpoint": {
        "id": "checkpoint-abc",
        "created_at": "2026-08-17T10:15:32+00:00",
        "unresolved_action_ref": "action-abc-123",
        "required_fresh_observation": true,
        "resume_reason": "verification_uncertain"
    },
    "unresolved_action": {
        "id": "action-abc-123",
        "type": "TAP",
        "params": {"x": 540, "y": 1200},
        "expected_outcome": "设置页面可见",
        "status": "UNCERTAIN"
    },
    "last_verification": {
        "verdict": "UNCERTAIN",
        "reason": "点击后屏幕未出现预期的设置页面标题",
        "created_at": "2026-08-17T10:15:31+00:00"
    },
    "last_observation": {
        "id": "observation-xyz",
        "captured_at": "2026-08-17T10:15:30+00:00",
        "screenshot_url": "/artifacts/...",
        "foreground_app": "com.android.settings"
    }
}
```

### 3.2 提交恢复决策

**补偿（Mark as Success）**：
```python
# POST /runtime/tasks/{task_id}/recovery/resolve
{
    "action": "compensate",
    "action_id": "action-abc-123",
    "verdict": "SUCCESS",
    "reason": "Manual verification confirms success",
    "verified_facts": [
        {
            "key": "settings.page.visible",
            "value": {"status": "visible"},
            "confidence": 1.0
        }
    ],
    "complete_stage": true
}
```

**重试（New Action）**：
```python
# POST /runtime/tasks/{task_id}/recovery/resolve
{
    "action": "retry",
    "action_id": "action-abc-123",
    "mark_as": "FAIL",
    "reason": "Manual review: will retry with new observation"
}
# 然后正常流程：capture_observation → propose_action
```

**跳过（Mark as Failed）**：
```python
# POST /runtime/tasks/{task_id}/recovery/resolve
{
    "action": "skip",
    "action_id": "action-abc-123",
    "mark_as": "FAIL",
    "reason": "Manual review: skip this action path"
}
```

**终止 Task**：
```python
# POST /runtime/tasks/{task_id}/close
{
    "reason": "Manual termination: unrecoverable UNCERTAIN state"
}
```

### 3.3 前端 UI 原型

**恢复面板**：
```
┌─────────────────────────────────────────────┐
│ ⚠️ Task 需要人工恢复                         │
├─────────────────────────────────────────────┤
│ 未解决的 Action:                             │
│   类型: TAP (540, 1200)                      │
│   预期: "设置页面可见"                        │
│   状态: UNCERTAIN                             │
│                                              │
│ 最后验证:                                     │
│   原因: "点击后屏幕未出现预期标题"             │
│   时间: 10:15:31                              │
│                                              │
│ 最后 Observation:                             │
│   [截图缩略图]                                │
│   前台应用: com.android.settings             │
│   时间: 10:15:30                              │
│                                              │
│ 决策选项:                                     │
│ [ 补偿：标记为成功 ]                          │
│ [ 重试：捕获新 Observation ]                  │
│ [ 跳过：标记为失败，尝试其他路径 ]             │
│ [ 终止：关闭 Task ]                           │
└─────────────────────────────────────────────┘
```

---

## 4. DeviceExecutionLease 恢复语义（Phase 5 前瞻）

### 4.1 Lease 与 Checkpoint 的关系

**问题**：
- Action 执行时持有 DeviceExecutionLease
- 进程崩溃时 Lease 可能还在数据库中
- 重启后如何处理孤立的 Lease？

**方案**：
```python
# Phase 5 设计草案
class DeviceExecutionLease:
    id: str
    device_id: str
    task_id: str
    action_id: str | None  # 如果正在执行 Action
    acquired_at: str
    expires_at: str
    process_id: str  # 持有进程的 PID
    
# 恢复逻辑
def recover_orphaned_leases():
    for lease in store.list_leases():
        if not is_process_alive(lease.process_id):
            # 检查关联的 Action
            if lease.action_id:
                action = store.load_action(lease.task_id, lease.action_id)
                if action.status == ActionStatus.PROPOSED:
                    # Action 已 propose 但未 execute，需要人工决策
                    create_checkpoint(
                        task_id=lease.task_id,
                        unresolved_action_ref=lease.action_id,
                        reason="process_crash_during_execution"
                    )
            # 释放 Lease
            store.release_lease(lease.id)
```

### 4.2 Lease 超时策略

**正常流程**：
```
1. acquire_lease(device_id, task_id, action_id, ttl=60s)
2. execute_action(device_id, action)
3. record_action_execution(...)
4. release_lease(lease_id)
```

**超时场景**：
- Lease 60 秒后自动过期
- 如果 Action 仍未 execute，创建 Checkpoint
- 如果 Action 已 execute 但未 verify，同样创建 Checkpoint

**设计目标**：
- Lease 超时 = 进程崩溃的等价信号
- 自动触发恢复流程

---

## 5. 实施计划

### Phase 4.5: 补充当前设计（本次）

1. ✅ 更新 `PHASE_4_ACTION_VERIFY_COMMIT_SPINE.md` 添加恢复语义章节
2. 🔄 决策 UNCERTAIN 自动 Checkpoint 策略
3. 🔄 设计人工恢复 API/UI 原型（本文档）
4. ⏸️ 评审后冻结设计

**输出**：
- 更新的 Phase 4 文档
- `PHASE_4_RECOVERY_STRATEGY.md`（本文档）

### Phase 5: Device Ownership + Real Execution

1. 实现 `DeviceExecutionLease`
2. 实现 Legacy Writer 排空
3. 实现 Real ADB 调用
4. 集成 Lease 恢复逻辑

### Phase 6: Gateway + Frontend

1. 实现 `/runtime/tasks/{id}/recovery` API
2. 实现恢复决策 UI
3. 实现 WebSocket 实时推送
4. 端到端测试

---

## 6. 开放问题

### 6.1 UNCERTAIN 自动 Checkpoint

**问题**：是否在 `verify_action(verdict=UNCERTAIN)` 时自动创建 Checkpoint？

**建议**：**是**，理由见 §1.3

**待确认**：你的决策？

### 6.2 自动补偿策略

**问题**：哪些 Action 类型可以自动重试？

**建议**：Phase 4 不实现，积累生产数据后 Phase 6+ 再设计

**待确认**：是否同意？

### 6.3 Lease 超时时间

**问题**：DeviceExecutionLease 的合理 TTL？

**建议**：
- 默认 60 秒（覆盖大部分 ADB 操作）
- 可配置（某些操作可能需要更长时间）

**待确认**：Phase 5 再细化

---

## 7. 总结

**本文档定义**：
1. UNCERTAIN 自动创建 Checkpoint（推荐）
2. 人工恢复决策流程（补偿/重试/跳过/终止）
3. Checkpoint 恢复 API 和 UI 原型
4. DeviceExecutionLease 恢复语义草案（Phase 5 前瞻）

**下一步**：
- 评审本文档
- 决策 UNCERTAIN 自动 Checkpoint
- 冻结 Phase 4 设计
- 开始 Phase 5 实施
