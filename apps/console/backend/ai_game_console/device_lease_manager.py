from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from collections.abc import Callable
from threading import Lock, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_kernel.lease import DeviceExecutionLease
    from .runtime_kernel.ports import RuntimeStorePort

logger = logging.getLogger(__name__)


class DeviceLeaseManager:
    """设备 Lease 生命周期管理器
    
    负责：
    1. Lease 获取和释放
    2. 后台清理过期和孤立的 Lease
    3. 进程存活检测
    4. 为孤立 Lease 创建恢复 Checkpoint
    """
    
    def __init__(
        self,
        store: RuntimeStorePort,
        clock: Callable[[], str],
        process_id: str | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._process_id = process_id or str(os.getpid())
        self._cleanup_thread: Thread | None = None
        self._cleanup_running = False
        # Week 6: 清理统计（后台线程与 API 线程共享）
        self._stats_lock = Lock()
        self._cleanup_runs = 0
        self._cleanup_errors = 0
    
    def start_background_cleanup(self, interval_seconds: int = 30) -> None:
        """启动后台线程清理孤立 Lease
        
        每隔 interval_seconds 秒检查：
        1. 已过期的 Lease
        2. 持有进程已死的 Lease
        """
        if self._cleanup_thread is not None:
            logger.warning("Background cleanup already running")
            return
        
        self._cleanup_running = True
        self._cleanup_thread = Thread(
            target=self._cleanup_loop,
            args=(interval_seconds,),
            daemon=True,
            name="DeviceLeaseCleanup",
        )
        self._cleanup_thread.start()
        logger.info(f"Started background lease cleanup (interval={interval_seconds}s)")
    
    def stop_background_cleanup(self) -> None:
        """停止后台清理线程"""
        if self._cleanup_thread is None:
            return
        
        self._cleanup_running = False
        self._cleanup_thread.join(timeout=5.0)
        self._cleanup_thread = None
        logger.info("Stopped background lease cleanup")
    
    def _cleanup_loop(self, interval_seconds: int) -> None:
        """后台清理循环"""
        while self._cleanup_running:
            with self._stats_lock:
                self._cleanup_runs += 1
            try:
                self._cleanup_orphaned_leases()
            except Exception as e:
                with self._stats_lock:
                    self._cleanup_errors += 1
                logger.error(f"Lease cleanup failed: {e}", exc_info=True)
            
            # 分段 sleep，便于快速停止
            for _ in range(interval_seconds):
                if not self._cleanup_running:
                    break
                time.sleep(1.0)
    
    def trigger_cleanup(self) -> dict[str, int]:
        """Week 6 手动干预：同步触发一轮清理（与后台线程同一逻辑）"""
        with self._stats_lock:
            self._cleanup_runs += 1
        try:
            return self._cleanup_orphaned_leases()
        except Exception as e:
            with self._stats_lock:
                self._cleanup_errors += 1
            logger.error(f"Manual lease cleanup failed: {e}", exc_info=True)
            return {
                "expired_found": 0,
                "deadline_exceeded": 0,
                "released": 0,
                "checkpointed": 0,
                "errors": 1,
            }
    
    def force_release(self, lease_id: str) -> dict[str, bool | str | None]:
        """Week 6 手动干预：强制释放指定 Lease
        
        若 Lease 关联了 Action，先创建恢复 Checkpoint（原因 manual_admin_release）。
        """
        lease = self._store.get_lease(lease_id)
        if lease is None:
            return {
                "lease_id": lease_id,
                "found": False,
                "released": False,
                "checkpointed": False,
                "error": None,
            }
        
        checkpointed = False
        if lease.action_id:
            try:
                self._create_recovery_checkpoint(
                    lease, resume_reason="manual_admin_release"
                )
                checkpointed = True
            except Exception as e:
                logger.error(
                    f"Failed to create recovery checkpoint for force-released "
                    f"lease {lease.id}: {e}",
                    exc_info=True,
                )
        
        try:
            self._store.release_lease(lease_id)
        except Exception as e:
            logger.error(f"Failed to force-release lease {lease_id}: {e}", exc_info=True)
            return {
                "lease_id": lease_id,
                "found": True,
                "released": False,
                "checkpointed": checkpointed,
                "error": str(e),
            }
        
        logger.info(f"Force-released lease {lease_id} by admin")
        return {
            "lease_id": lease_id,
            "found": True,
            "released": True,
            "checkpointed": checkpointed,
            "error": None,
        }
    
    def cleanup_stats(self) -> dict[str, int]:
        """Week 6: 清理统计（runs/errors）"""
        with self._stats_lock:
            return {"runs": self._cleanup_runs, "errors": self._cleanup_errors}
    
    def is_process_alive(self, process_id: str) -> bool:
        """Week 6: 公开进程存活检测（管理查询用）"""
        return self._is_process_alive(process_id)
    
    def _cleanup_orphaned_leases(self) -> dict[str, int]:
        """清理孤立 Lease（进程已死或已过期），返回清理摘要

        Week 5 Deadline 保护：deadline 超限的 Lease 无论进程是否存活都立即释放
        （续期窗口已关闭，不再无限延长），Checkpoint 原因标记 deadline_exceeded。
        """
        summary = {
            "expired_found": 0,
            "deadline_exceeded": 0,
            "released": 0,
            "checkpointed": 0,
            "errors": 0,
        }
        try:
            now = self._clock()
            expired = self._store.list_expired_leases(now)
            
            if not expired:
                return summary
            
            summary["expired_found"] = len(expired)
            logger.info(f"Found {len(expired)} expired leases, checking process liveness")
            
            for lease in expired:
                deadline_exceeded = lease.is_deadline_exceeded(now)
                
                if deadline_exceeded:
                    # Week 5: 绝对 Deadline 已超限，续期不再允许，立即释放
                    summary["deadline_exceeded"] += 1
                    logger.warning(
                        f"Lease {lease.id} exceeded deadline {lease.deadline_at} "
                        f"(device={lease.device_id}, task={lease.task_id}, "
                        f"holder={lease.holder_process_id}); releasing without renewal"
                    )
                elif self._is_process_alive(lease.holder_process_id):
                    # 进程还活着，但 Lease 过期了（可能是续期失败）
                    logger.warning(
                        f"Lease {lease.id} expired but process {lease.holder_process_id} "
                        f"is still alive (device={lease.device_id}, task={lease.task_id})"
                    )
                else:
                    # 进程已死
                    logger.warning(
                        f"Lease {lease.id} orphaned: process {lease.holder_process_id} dead "
                        f"(device={lease.device_id}, task={lease.task_id})"
                    )
                
                # 如果有关联 Action，创建恢复 Checkpoint
                if lease.action_id:
                    try:
                        self._create_recovery_checkpoint(
                            lease,
                            resume_reason=(
                                "deadline_exceeded"
                                if deadline_exceeded
                                else "process_crash_during_lease_hold"
                            ),
                        )
                        summary["checkpointed"] += 1
                    except Exception as e:
                        summary["errors"] += 1
                        logger.error(
                            f"Failed to create recovery checkpoint for lease {lease.id}: {e}",
                            exc_info=True,
                        )
                
                # 释放 Lease
                try:
                    self._store.release_lease(lease.id)
                    summary["released"] += 1
                    logger.info(f"Released orphaned lease {lease.id}")
                except Exception as e:
                    summary["errors"] += 1
                    logger.error(f"Failed to release lease {lease.id}: {e}", exc_info=True)
        except Exception as e:
            summary["errors"] += 1
            logger.error(f"Error during lease cleanup: {e}", exc_info=True)
        return summary
    
    def _is_process_alive(self, process_id: str) -> bool:
        """检查进程是否存在（跨平台）"""
        try:
            pid = int(process_id)
        except ValueError:
            return False
        
        if sys.platform == "win32":
            # Windows: 尝试打开进程句柄
            try:
                # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                return False
        else:
            # Unix: 发送信号 0
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    
    def _create_recovery_checkpoint(
        self,
        lease: DeviceExecutionLease,
        *,
        resume_reason: str = "process_crash_during_lease_hold",
    ) -> None:
        """为孤立 Lease 创建恢复 Checkpoint
        
        当进程崩溃时，如果 Lease 关联了 Action，说明 Action 可能：
        1. 已 propose 但未 execute（状态 = PROPOSED）
        2. 已 execute 但未 verify（状态 = EXECUTED）
        
        两种情况都需要创建 Checkpoint 标记 unresolved_action_ref。
        resume_reason 区分崩溃（默认）、Deadline 超限（deadline_exceeded）
        和管理员手动释放（manual_admin_release）。
        """
        from uuid import uuid4
        from .runtime_kernel.action import ActionStatus
        from .runtime_kernel.checkpoint import CheckpointDraft
        
        # 加载 Task 和 Action
        task = self._store.load_task(lease.task_id)
        action = self._store.load_action(lease.task_id, lease.action_id)
        
        # 只有 PROPOSED 或 EXECUTED 状态需要恢复
        if action.status not in (ActionStatus.PROPOSED, ActionStatus.EXECUTED):
            logger.info(
                f"Action {action.id} is {action.status.value}, no recovery needed"
            )
            return
        
        # 检查是否已存在相同 unresolved_action_ref 的 Checkpoint（去重）
        latest_checkpoint = self._store.latest_checkpoint(lease.task_id)
        if latest_checkpoint and latest_checkpoint.unresolved_action_ref == lease.action_id:
            logger.info(
                f"Checkpoint already exists for unresolved action {lease.action_id}, skipping"
            )
            return
        
        # 构建 Checkpoint
        stages = self._store.list_stages(lease.task_id)
        verified_facts = self._store.list_facts(lease.task_id, verified_only=True)
        
        completed_summaries = tuple(
            {
                "id": stage.id,
                "ordinal": stage.ordinal,
                "objective": stage.objective,
                "progress_summary": stage.progress_summary,
                "completed_at": stage.completed_at,
                "evidence_refs": list(stage.evidence_refs),
            }
            for stage in stages
            if stage.status.value == "COMPLETED"
        )
        
        observation = self._store.load_observation(task.last_observation_id) if task.last_observation_id else None
        device_summary = {
            "device_id": task.device_id,
            "last_observation_id": task.last_observation_id,
        }
        if observation:
            device_summary.update({
                "foreground_app": observation.device_state.foreground_app,
                "connection_state": observation.device_state.connection_state.value,
                "captured_at": observation.captured_at,
            })
        
        checkpoint_draft = CheckpointDraft(
            id=str(uuid4()),
            task_id=task.id,
            goal=task.goal,
            status_at_checkpoint=task.status,
            current_stage_id=task.current_stage_id,
            completed_stage_summaries=completed_summaries,
            verified_facts=verified_facts,
            device_summary=device_summary,
            last_meaningful_progress=(
                {"at": task.last_meaningful_progress_at}
                if task.last_meaningful_progress_at
                else None
            ),
            failure_summary=(
                {
                    "code": task.failure_state.code,
                    "summary": task.failure_state.summary,
                    "retry_count": task.failure_state.retry_count,
                    "no_progress_count": task.failure_state.no_progress_count,
                    "last_failed_action_id": task.failure_state.last_failed_action_id,
                    "last_verdict": task.failure_state.last_verdict,
                    "recoverable": task.failure_state.recoverable,
                    "updated_at": task.failure_state.updated_at,
                }
                if task.failure_state
                else None
            ),
            resume_reason=resume_reason,
            required_fresh_observation=True,
            unresolved_action_ref=lease.action_id,
            created_at=self._clock(),
        )
        
        # 更新 Task 记录 Checkpoint
        after_task = task.record_checkpoint(checkpoint_draft.id, at=self._clock())
        
        # 持久化 Checkpoint
        from .runtime_kernel.event import RuntimeEventDraft, EventActor
        event = RuntimeEventDraft(
            id=str(uuid4()),
            type="CheckpointCreated",
            actor=EventActor.RUNTIME,
            payload={
                "checkpoint_id": checkpoint_draft.id,
                "reason": checkpoint_draft.resume_reason,
                "required_fresh_observation": checkpoint_draft.required_fresh_observation,
                "unresolved_action_ref": checkpoint_draft.unresolved_action_ref,
            },
            correlation_id=task.id,
            created_at=self._clock(),
        )
        
        checkpoint, _ = self._store.create_checkpoint(
            before_task=task,
            after_task=after_task,
            checkpoint=checkpoint_draft,
            event=event,
        )
        
        logger.info(
            f"Created recovery checkpoint {checkpoint.id} for task {task.id}, "
            f"unresolved action {lease.action_id}"
        )
