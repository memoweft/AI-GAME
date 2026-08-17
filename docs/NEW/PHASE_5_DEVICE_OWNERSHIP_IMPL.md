# Phase 5 — Device Ownership Implementation Plan

**状态**: DESIGN DRAFT  
**日期**: 2026-08-17  
**依据**: `PHASE_1_DEVICE_OWNERSHIP.md` (FROZEN DESIGN)  
**前置**: Phase 4 Action→Verify→Commit spine complete

---

## 1. 目标与边界

实现 `PHASE_1_DEVICE_OWNERSHIP.md` 中定义的所有权不变量和 Lease 生命周期，使 Runtime Kernel 成为唯一有权向目标 Android 设备发出自动 Action 的核心。

**本阶段交付**：
1. `DeviceExecutionLease` 持久化模型和独占逻辑
2. Legacy Writer 排空机制（LEGACY_ACTIVE → DRAINING → KERNEL_ACTIVE）
3. Real ADB 执行集成（tap/swipe/input_text/back/home）
4. Action Executor Port 实现
5. Lease 恢复逻辑（孤立 Lease 检测和清理）

**边界**：
- ✅ 实现真实设备执行能力
- ✅ 实现设备独占控制
- ✅ 集成现有 `adb_executor.py`
- ⏸️ 不实现 Gateway 消息路由
- ⏸️ 不实现前端 Pause/Resume/Cancel UI
- ⏸️ 不实现多 Client 协调（Gateway 层的工作）
- ⏸️ 不实现外部 owner 互斥（F:\dating-copilot）

---

## 2. DeviceExecutionLease 设计

### 2.1 数据模型

```python
@dataclass(frozen=True, slots=True)
class DeviceExecutionLease:
    """设备执行独占权证明"""
    id: str
    device_id: str
    task_id: str
    holder_process_id: str  # 当前进程 PID
    acquired_at: str
    expires_at: str  # TTL 默认 60 秒
    last_heartbeat_at: str  # 用于检测孤立 Lease
    action_id: str | None  # 当前执行的 Action（如果有）
```

### 2.2 SQLite Schema (revision 4)

```sql
CREATE TABLE IF NOT EXISTS runtime_device_leases (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    holder_process_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    action_id TEXT,
    
    UNIQUE(device_id),  -- 一个设备同时只能有一个 Lease
    FOREIGN KEY(task_id) REFERENCES runtime_tasks(id)
);

CREATE INDEX idx_runtime_device_leases_task 
    ON runtime_device_leases(task_id);
CREATE INDEX idx_runtime_device_leases_expires 
    ON runtime_device_leases(expires_at);
```

### 2.3 Lease 生命周期 API

```python
class RuntimeStorePort(Protocol):
    def acquire_lease(
        self,
        device_id: str,
        task_id: str,
        holder_process_id: str,
        ttl_seconds: int = 60,
    ) -> DeviceExecutionLease:
        """获取设备独占权，如果已被占用则抛出 LeaseConflict"""
        
    def renew_lease(self, lease_id: str) -> DeviceExecutionLease:
        """续期 Lease，更新 last_heartbeat_at 和 expires_at"""
        
    def release_lease(self, lease_id: str) -> None:
        """释放 Lease"""
        
    def get_lease_for_device(self, device_id: str) -> DeviceExecutionLease | None:
        """查询设备当前 Lease（如果有）"""
        
    def get_lease_for_task(self, task_id: str) -> DeviceExecutionLease | None:
        """查询 Task 当前 Lease（如果有）"""
        
    def list_expired_leases(self, now: str) -> tuple[DeviceExecutionLease, ...]:
        """查询所有已过期的 Lease"""
        
    def update_lease_action(self, lease_id: str, action_id: str | None) -> None:
        """更新 Lease 关联的 Action（用于恢复检测）"""
```

### 2.4 Lease 管理器

```python
class DeviceLeaseManager:
    """设备 Lease 生命周期管理"""
    
    def __init__(
        self,
        store: RuntimeStorePort,
        clock: Callable[[], str],
        process_id: str,
    ) -> None:
        self._store = store
        self._clock = clock
        self._process_id = process_id
        self._background_cleaner: Thread | None = None
        
    def acquire(
        self,
        device_id: str,
        task_id: str,
        ttl_seconds: int = 60,
    ) -> LeaseContext:
        """获取 Lease 并返回上下文管理器"""
        lease = self._store.acquire_lease(
            device_id=device_id,
            task_id=task_id,
            holder_process_id=self._process_id,
            ttl_seconds=ttl_seconds,
        )
        return LeaseContext(self._store, lease)
        
    def start_background_cleanup(self, interval_seconds: int = 30) -> None:
        """启动后台线程清理孤立 Lease"""
        if self._background_cleaner is not None:
            return
        self._background_cleaner = Thread(
            target=self._cleanup_loop,
            args=(interval_seconds,),
            daemon=True,
        )
        self._background_cleaner.start()
        
    def _cleanup_loop(self, interval_seconds: int) -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                self._cleanup_orphaned_leases()
            except Exception as e:
                logger.error(f"Lease cleanup failed: {e}")
                
    def _cleanup_orphaned_leases(self) -> None:
        """清理孤立 Lease（进程已死或已过期）"""
        now = self._clock()
        expired = self._store.list_expired_leases(now)
        for lease in expired:
            if not self._is_process_alive(lease.holder_process_id):
                logger.warning(
                    f"Lease {lease.id} orphaned (process {lease.holder_process_id} dead)"
                )
                # 如果有关联 Action，创建恢复 Checkpoint
                if lease.action_id:
                    self._create_recovery_checkpoint(lease)
                self._store.release_lease(lease.id)
                
    def _is_process_alive(self, process_id: str) -> bool:
        """检查进程是否存在（跨平台）"""
        try:
            pid = int(process_id)
            if sys.platform == "win32":
                # Windows: 尝试打开进程句柄
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                # Unix: 发送信号 0
                os.kill(pid, 0)
                return True
        except (ValueError, OSError):
            return False
            
    def _create_recovery_checkpoint(self, lease: DeviceExecutionLease) -> None:
        """为孤立 Lease 创建恢复 Checkpoint"""
        # 加载 Task 和 Action
        task = self._store.load_task(lease.task_id)
        action = self._store.load_action(lease.task_id, lease.action_id)
        
        # 如果 Action 还是 PROPOSED，说明还没真正执行
        if action.status == ActionStatus.PROPOSED:
            # 创建 Checkpoint 标记需要人工决策
            checkpoint_draft = CheckpointDraft(
                id=str(uuid4()),
                task_id=task.id,
                goal=task.goal,
                status_at_checkpoint=task.status,
                current_stage_id=task.current_stage_id,
                completed_stage_summaries=...,
                verified_facts=...,
                device_summary=...,
                last_meaningful_progress=...,
                failure_summary=...,
                resume_reason="process_crash_during_lease_hold",
                required_fresh_observation=True,
                unresolved_action_ref=lease.action_id,
                created_at=self._clock(),
            )
            # 持久化 Checkpoint
            after_task = task.record_checkpoint(checkpoint_draft.id, at=self._clock())
            self._store.create_checkpoint(
                before_task=task,
                after_task=after_task,
                checkpoint=checkpoint_draft,
                event=...,
            )


class LeaseContext:
    """Lease 上下文管理器（自动续期和释放）"""
    
    def __init__(self, store: RuntimeStorePort, lease: DeviceExecutionLease) -> None:
        self._store = store
        self._lease = lease
        self._renew_timer: Timer | None = None
        
    def __enter__(self) -> DeviceExecutionLease:
        # 启动自动续期（每 30 秒）
        self._renew_timer = Timer(30.0, self._renew_lease)
        self._renew_timer.daemon = True
        self._renew_timer.start()
        return self._lease
        
    def __exit__(self, *args) -> None:
        if self._renew_timer:
            self._renew_timer.cancel()
        self._store.release_lease(self._lease.id)
        
    def _renew_lease(self) -> None:
        try:
            self._lease = self._store.renew_lease(self._lease.id)
            # 继续下一次续期
            self._renew_timer = Timer(30.0, self._renew_lease)
            self._renew_timer.daemon = True
            self._renew_timer.start()
        except Exception as e:
            logger.error(f"Lease renewal failed: {e}")
```

---

## 3. Action Executor Port 设计

### 3.1 接口定义

```python
class ActionExecutorPort(Protocol):
    """设备 Action 执行端口（ADB 适配器）"""
    
    def execute_tap(
        self,
        device_id: str,
        x: int,
        y: int,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备坐标 (x, y) 处执行点击"""
        
    def execute_swipe(
        self,
        device_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备上执行滑动"""
        
    def execute_input_text(
        self,
        device_id: str,
        text: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备上输入文本"""
        
    def execute_back(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """执行返回键"""
        
    def execute_home(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """执行主页键"""


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    """Action 执行结果"""
    accepted: bool  # transport 是否接受
    adapter_code: int  # ADB 返回码
    error: ExecutionError | None  # 错误详情（如果有）
    started_at: str
    finished_at: str
```

### 3.2 ADB 适配器实现

```python
class AdbActionExecutor:
    """基于现有 adb_executor.py 的 Action 执行器"""
    
    def __init__(self, adb_path: str) -> None:
        self._adb_path = adb_path
        
    def execute_tap(
        self,
        device_id: str,
        x: int,
        y: int,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        started_at = _utc_now()
        try:
            # 调用现有 adb_executor
            result = subprocess.run(
                [self._adb_path, "-s", device_id, "shell", "input", "tap", str(x), str(y)],
                timeout=timeout_ms / 1000.0,
                capture_output=True,
                check=False,
            )
            finished_at = _utc_now()
            
            if result.returncode == 0:
                return ActionExecutionResult(
                    accepted=True,
                    adapter_code=result.returncode,
                    error=None,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            else:
                return ActionExecutionResult(
                    accepted=False,
                    adapter_code=result.returncode,
                    error=ExecutionError(
                        code="adb_command_failed",
                        message=result.stderr.decode("utf-8", errors="replace"),
                        retryable=True,
                    ),
                    started_at=started_at,
                    finished_at=finished_at,
                )
        except subprocess.TimeoutExpired:
            return ActionExecutionResult(
                accepted=False,
                adapter_code=-1,
                error=ExecutionError(
                    code="adb_timeout",
                    message=f"ADB tap timeout after {timeout_ms}ms",
                    retryable=True,
                ),
                started_at=started_at,
                finished_at=_utc_now(),
            )
        except Exception as e:
            return ActionExecutionResult(
                accepted=False,
                adapter_code=-1,
                error=ExecutionError(
                    code="adb_execution_error",
                    message=str(e),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=_utc_now(),
            )
            
    # 类似实现 execute_swipe, execute_input_text, execute_back, execute_home
```

---

## 4. Runtime Kernel 集成

### 4.1 扩展 RuntimeKernel

```python
class RuntimeKernel:
    def __init__(
        self,
        store: RuntimeStorePort,
        *,
        observation_provider: ObservationProviderPort | None = None,
        artifact_store: ArtifactStorePort | None = None,
        action_executor: ActionExecutorPort | None = None,  # 新增
        lease_manager: DeviceLeaseManager | None = None,  # 新增
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._observation_provider = observation_provider
        self._artifact_store = artifact_store
        self._action_executor = action_executor  # 新增
        self._lease_manager = lease_manager  # 新增
        self._clock = clock or _utc_now
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._store.initialize()
        
    def execute_action(
        self,
        *,
        task_id: str,
        action_id: str,
    ) -> ActionExecution:
        """执行 Action 的完整栅栏检查 + 物理下发"""
        if self._action_executor is None:
            raise RuntimeError("action executor is not configured")
        if self._lease_manager is None:
            raise RuntimeError("lease manager is not configured")
            
        # 1. prepare_action_execution() 栅栏检查
        action = self.prepare_action_execution(task_id=task_id, action_id=action_id)
        
        # 2. 获取设备独占 Lease
        task = self._store.load_task(task_id)
        with self._lease_manager.acquire(task.device_id, task_id) as lease:
            # 3. 更新 Lease 关联 Action（用于恢复检测）
            self._store.update_lease_action(lease.id, action_id)
            
            # 4. 根据 Action 类型调用对应执行器
            if action.type == ActionType.TAP:
                result = self._action_executor.execute_tap(
                    device_id=task.device_id,
                    x=action.params["x"],
                    y=action.params["y"],
                )
            elif action.type == ActionType.SWIPE:
                result = self._action_executor.execute_swipe(
                    device_id=task.device_id,
                    start_x=action.params["start_x"],
                    start_y=action.params["start_y"],
                    end_x=action.params["end_x"],
                    end_y=action.params["end_y"],
                    duration_ms=action.params.get("duration_ms", 300),
                )
            elif action.type == ActionType.INPUT_TEXT:
                result = self._action_executor.execute_input_text(
                    device_id=task.device_id,
                    text=action.params["text"],
                )
            elif action.type == ActionType.BACK:
                result = self._action_executor.execute_back(
                    device_id=task.device_id,
                )
            elif action.type == ActionType.HOME:
                result = self._action_executor.execute_home(
                    device_id=task.device_id,
                )
            else:
                raise ValueError(f"unsupported action type: {action.type}")
                
            # 5. 清除 Lease 的 Action 关联
            self._store.update_lease_action(lease.id, None)
            
            # 6. 持久化执行结果
            execution = self.record_action_execution(
                task_id=task_id,
                action_id=action_id,
                accepted=result.accepted,
                adapter_code=result.adapter_code,
                error=result.error,
                started_at=result.started_at,
                finished_at=result.finished_at,
                lease_ref=lease.id,
            )
            
        # Lease 自动释放
        return execution
```

---

## 5. Legacy Writer 排空机制

### 5.1 三阶段切换

**阶段 1: LEGACY_ACTIVE**
- 当前状态：旧 Runtime 运行，新 Kernel 不执行
- 代码标记：配置文件 `runtime_mode: "legacy"`
- 行为：
  - `DeviceLeaseManager` 不启动
  - `RuntimeKernel.execute_action()` 抛出 `RuntimeError("kernel not active")`
  - 旧 `MobileTask` 按现状运行

**阶段 2: DRAINING**
- 配置文件：`runtime_mode: "draining"`
- 行为：
  - 拒绝创建新的 `MobileTask`（API 返回 503）
  - 等待现有 `MobileTask` 完成
  - `DeviceLeaseManager` 启动但不 acquire
  - 每 30 秒检查：`SELECT COUNT(*) FROM mobile_tasks WHERE status IN ('running', 'pending')`
  - 如果 count = 0，记录日志 "legacy drain complete"

**阶段 3: KERNEL_ACTIVE**
- 配置文件：`runtime_mode: "kernel_active"`
- 行为：
  - `RuntimeKernel.execute_action()` 正常工作
  - 旧 `MobileTask` API 固定返回 410 Gone
  - `DeviceLeaseManager` 正常 acquire
  - Legacy direct executor route 禁用

### 5.2 配置文件

```yaml
# config/runtime.yml
runtime:
  mode: "legacy"  # legacy | draining | kernel_active
  kernel:
    lease_ttl_seconds: 60
    lease_cleanup_interval_seconds: 30
    action_timeout_ms: 5000
```

### 5.3 启动检查

```python
def validate_runtime_mode(config: Settings) -> None:
    """启动时验证 runtime mode 一致性"""
    mode = config.runtime.mode
    
    if mode == "legacy":
        # 确保 Kernel 不启动
        if config.runtime.kernel.enabled:
            raise ConfigError("kernel cannot be enabled in legacy mode")
            
    elif mode == "draining":
        # 允许 Kernel 存在但不执行
        logger.info("Runtime in DRAINING mode, waiting for legacy tasks to complete")
        
    elif mode == "kernel_active":
        # 确保 Legacy 禁用
        if config.runtime.legacy.enabled:
            raise ConfigError("legacy runtime must be disabled in kernel_active mode")
        logger.info("Runtime in KERNEL_ACTIVE mode, Kernel owns device execution")
        
    else:
        raise ConfigError(f"unknown runtime mode: {mode}")
```

---

## 6. 测试策略

### 6.1 单元测试

**Lease 独占性**:
```python
def test_lease_prevents_concurrent_acquisition(tmp_path: Path) -> None:
    """验证同一设备不能被两个 Task 同时 acquire"""
    manager = DeviceLeaseManager(store, clock, "process-1")
    
    lease1 = manager.acquire("device-1", "task-1")
    with pytest.raises(LeaseConflict):
        manager.acquire("device-1", "task-2")
```

**Lease 过期清理**:
```python
def test_expired_lease_is_cleaned_up(tmp_path: Path) -> None:
    """验证过期 Lease 被后台线程清理"""
    manager = DeviceLeaseManager(store, clock, "process-1")
    manager.start_background_cleanup(interval_seconds=1)
    
    lease = store.acquire_lease("device-1", "task-1", "process-1", ttl_seconds=2)
    time.sleep(3)
    
    # Lease 应该已被清理
    assert store.get_lease_for_device("device-1") is None
```

**孤立 Lease 恢复**:
```python
def test_orphaned_lease_creates_recovery_checkpoint(tmp_path: Path) -> None:
    """验证孤立 Lease 自动创建恢复 Checkpoint"""
    # 模拟进程死亡场景
    lease = store.acquire_lease("device-1", "task-1", "dead-process", ttl_seconds=60)
    store.update_lease_action(lease.id, "action-1")
    
    # 触发清理（模拟进程检测为已死）
    manager._cleanup_orphaned_leases()
    
    # 验证 Checkpoint 已创建
    checkpoint = store.latest_checkpoint("task-1")
    assert checkpoint is not None
    assert checkpoint.unresolved_action_ref == "action-1"
```

### 6.2 集成测试

**完整执行流程**:
```python
def test_execute_action_full_flow(tmp_path: Path) -> None:
    """验证 execute_action() 完整流程：栅栏检查 → Lease → 执行 → 记录"""
    kernel = RuntimeKernel(
        store,
        action_executor=FakeAdbExecutor(),
        lease_manager=DeviceLeaseManager(store, clock, str(os.getpid())),
    )
    
    # 准备 Task 和 Action
    task = kernel.create_task(...)
    stage = kernel.create_stage(...)
    kernel.start_stage(...)
    obs = kernel.capture_observation(...)
    action = kernel.propose_action(...)
    
    # 执行 Action
    execution = kernel.execute_action(task_id=task.id, action_id=action.id)
    
    # 验证结果
    assert execution.accepted is True
    assert kernel.load_action(task.id, action.id).status == ActionStatus.EXECUTED
```

**Legacy 排空**:
```python
def test_draining_mode_rejects_new_legacy_tasks() -> None:
    """验证 DRAINING 模式拒绝新 Legacy Task"""
    config = Settings(runtime=RuntimeConfig(mode="draining"))
    app = create_app(config)
    
    response = app.post("/api/mobile_tasks", json={...})
    assert response.status_code == 503
```

---

## 7. 实施计划

### Week 1: DeviceExecutionLease
- [ ] SQLite schema revision 4
- [ ] `DeviceExecutionLease` domain model
- [ ] Store port 扩展（acquire/renew/release/list）
- [ ] `DeviceLeaseManager` 实现
- [ ] 单元测试：独占性、过期清理、孤立 Lease 恢复

### Week 2: Action Executor
- [ ] `ActionExecutorPort` 接口定义
- [ ] `AdbActionExecutor` 实现（基于现有 `adb_executor.py`）
- [ ] `RuntimeKernel.execute_action()` 集成
- [ ] 单元测试：各 Action 类型执行
- [ ] 集成测试：完整执行流程

### Week 3: Legacy 排空
- [ ] 配置文件 `runtime_mode` 支持
- [ ] DRAINING 模式实现（拒绝新 Legacy Task）
- [ ] KERNEL_ACTIVE 模式实现（禁用 Legacy API）
- [ ] 启动检查和日志
- [ ] 集成测试：三阶段切换

### Week 4: 端到端验证
- [ ] 真实设备冒烟测试（tap/swipe/back/home）
- [ ] Lease 并发冲突测试
- [ ] 进程崩溃恢复测试
- [ ] 性能测试：Lease 续期开销
- [ ] 文档：部署指南和运维手册

---

## 8. 风险与缓解

### 风险 1: ADB 命令超时或失败率高
**缓解**:
- 使用现有 `adb_executor.py` 的超时和重试逻辑
- 记录详细的 ADB 错误码和消息
- UNCERTAIN verdict 触发人工介入

### 风险 2: Lease 续期失败导致意外释放
**缓解**:
- 自动续期间隔（30 秒）远小于 TTL（60 秒）
- 续期失败时记录错误日志
- 考虑增加续期重试逻辑

### 风险 3: Legacy 排空期间新任务积压
**缓解**:
- DRAINING 模式返回 503（客户端应该重试）
- 提供管理 API 查询排空状态
- 监控 Legacy Task 数量，超时自动切换

### 风险 4: 外部 owner (F:\dating-copilot) 并发控制
**缓解**:
- Phase 5 不处理外部 owner（明确边界）
- 文档标记需要后续运维协调
- 如果发现冲突，回滚到 DRAINING 模式

---

## 9. 成功标准

Phase 5 完成的定义：
- ✅ 470 + 新增测试全部通过
- ✅ 真实设备上执行 tap/swipe/back/home 成功
- ✅ 两个 Task 无法同时 acquire 同一设备
- ✅ Lease 过期后自动清理
- ✅ 进程崩溃后孤立 Lease 创建恢复 Checkpoint
- ✅ DRAINING → KERNEL_ACTIVE 切换不丢失任务
- ✅ 文档完整：API、配置、部署、故障排查

---

## 10. 下一步

审阅本文档后：
1. 确认 Phase 5 范围和边界
2. 确认实施计划和时间表
3. 开始 Week 1 实施
