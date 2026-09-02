from __future__ import annotations

import hashlib
import logging
import re
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from ..agent_runtime.domain import Task
from ..agent_runtime.scheduler import (
    OverlapPolicy,
    SQLiteSchedulerCoordination,
    WakeDispatch,
    stable_wake_id,
)
from ..agent_runtime.service import CanonicalTaskService
from ..agent_runtime.store import TaskExpectedStatusConflict
from .domain import EmulatorSettingsRun, SettingsRunStatus
from .service import IDENTITY_PROFILE_ERROR_CODES, EmulatorProfileServiceError


RUNNER_KIND = "emulator_settings_v1"
RUNNER_VERSION = "1"
SETTINGS_SUBTASK_KIND = "emulator_settings_v1"
GENERAL_RUNNER_KIND = "android_ui_agent"
GENERAL_RUNNER_VERSION = "1"
GENERAL_SUBTASK_KIND = "android_ui_agent"
_CONTROLLED = frozenset({"paused", "user_takeover", "cancelled"})
_RESUMABLE = frozenset({"running", "recovering", "replanning"})
_PREFLIGHT_STATUSES = frozenset({"scheduled", "waiting_time", "running", "recovering", "replanning"})
_OPERATOR_IDENTITY_ERROR_CODES = frozenset(
    {
        "emulator_identity_mismatch",
        "emulator_profile_identity_mismatch",
        "emulator_profile_owner_mismatch",
        "emulator_profile_binding_mismatch",
        "emulator_profile_generation_mismatch",
        "emulator_canonical_identity_mismatch",
        "canonical_profile_binding_mismatch",
        "canonical_profile_binding_missing",
        "canonical_profile_binding_duplicate",
        "canonical_profile_binding_malformed",
    }
)
_RUNNER_ERROR_CODE = re.compile(r"[a-z0-9_]{1,120}\Z")
_LOGGER = logging.getLogger(__name__)


def initial_wake_id(task_id: str) -> str:
    return f"task:{task_id}:runner:{RUNNER_KIND}:v{RUNNER_VERSION}:initial"


def settings_action_identity(task_id: str, subtask_id: str) -> str:
    digest = hashlib.sha256(
        f"{task_id}\x00{subtask_id}\x00{RUNNER_KIND}\x00{RUNNER_VERSION}".encode("utf-8")
    ).hexdigest()
    return f"emulator-settings-v1:{digest}"


def general_initial_wake_id(task_id: str) -> str:
    return f"task:{task_id}:runner:{GENERAL_RUNNER_KIND}:v{GENERAL_RUNNER_VERSION}:initial"


def _safe_runner_error_code(error: Exception) -> str:
    """Expose only a stable local error code for recoverable runner diagnosis."""

    value = str(error).strip()
    if _RUNNER_ERROR_CODE.fullmatch(value):
        return value
    frames = traceback.extract_tb(error.__traceback__)
    if frames:
        leaf = frames[-1]
        return f"{type(error).__name__}@{leaf.name}:{leaf.lineno}"
    return type(error).__name__


class ResidentV2TaskScheduler:
    """Multi-owner Android UI runner driven only by AgentRuntimeEventPump.

    The runtime store is used once per poll for exhaustive read-only discovery.
    Every inspect and mutation after discovery goes through a
    ``CanonicalTaskService`` built from the Task's persisted owner pair.
    """

    def __init__(
        self,
        *,
        runtime_store: Any,
        coordination: SQLiteSchedulerCoordination,
        profiles: Any,
        operator: Any | None,
        owner_id: str,
        clock: Callable[[], datetime] | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
        recovery_backoff: timedelta = timedelta(seconds=5),
        general_step_backoff: timedelta = timedelta(milliseconds=100),
        runner_admission: Any | None = None,
        general_handler: Any | None = None,
        checkpoint: Callable[[str, str], None] | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("resident scheduler owner_id is required")
        if (
            lease_ttl <= timedelta(0)
            or recovery_backoff <= timedelta(0)
            or general_step_backoff <= timedelta(0)
        ):
            raise ValueError("resident scheduler timing must be positive")
        self.runtime_store = runtime_store
        self.coordination = coordination
        self.profiles = profiles
        self.operator = operator
        self.owner_id = owner_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_ttl = lease_ttl
        self.recovery_backoff = recovery_backoff
        self.general_step_backoff = general_step_backoff
        self.runner_admission = runner_admission
        self.general_handler = general_handler
        self.checkpoint = checkpoint or (lambda _name, _task_id: None)

    def poll_once(self) -> int:
        now = self._now()
        if not self._lease(now):
            return 0
        handled = 0
        for candidate in self.runtime_store.list_tasks_for_scheduler():
            # Refresh before every candidate; a stale process that lost its
            # lease must stop scheduling.  The downstream command claim remains
            # the final at-most-once fence if a poll overlaps lease expiry.
            if not self._lease(self._now()):
                break
            handled += self._poll_task(candidate, self._now())
        return handled

    def shutdown(self) -> None:
        self.coordination.release_lease(owner_id=self.owner_id)

    def _poll_task(self, candidate: Task, now: datetime) -> int:
        if self._has_exact_general_marker(candidate):
            return self._poll_general_task(candidate, now)
        return 0

    def _poll_settings_task(self, candidate: Task, now: datetime) -> int:
        if self.runner_admission is not None and self._admission(
            candidate, runner_kind=RUNNER_KIND, runner_version=RUNNER_VERSION,
        ) is None:
            return 0
        service = CanonicalTaskService(
            self.runtime_store,
            principal_id=candidate.owner_principal_id,
            controller_id=candidate.controller_id,
        )
        task = service.inspect_task(candidate.id)
        status = str(task["status"])
        active = self.coordination.active_wake(task_id=candidate.id)
        if bool(task.get("terminal")) or status == "cancelled":
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
            return 0

        binding = self._binding(task)
        if binding is None:
            # A marked Task with a missing/malformed/duplicate binding is an
            # integrity defect, but it must never cross the scheduler claim or
            # operator boundary.
            service.fence_integrity(
                candidate.id,
                reason_code="identity_crosswire",
                summary="The Settings runner binding is not uniquely authorized.",
                idempotency_key=f"runner:{candidate.id}:binding-invalid",
            )
            return 0
        subtask, profile_id = binding
        action_id = settings_action_identity(candidate.id, str(subtask["subtask_id"]))

        if status == "paused":
            return 0
        if status == "user_takeover":
            if active is not None:
                try:
                    self.operator.takeover(
                        principal_id=candidate.owner_principal_id,
                        controller_id=candidate.controller_id,
                        task_id=candidate.id,
                        action_id=action_id,
                    )
                except EmulatorProfileServiceError:
                    pass
                service.upsert_subtask(
                    candidate.id,
                    kind=SETTINGS_SUBTASK_KIND,
                    object_ref=profile_id,
                    conversation_ref=None,
                    status="user_takeover",
                    priority=int(task["priority"]),
                    current_stage="user_takeover",
                )
            return 0

        if status not in _PREFLIGHT_STATUSES:
            return 0

        if status == "waiting_time":
            # Preserve the established semantics: a completed retry wake is
            # settled before checking its due time, then an early poll is
            # completely inert.  Never hand this completed wake to _wake.
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
                active = None
            due_at = task.get("next_wake_at")
            if not isinstance(due_at, str) or _parse_utc(due_at) > now:
                return 0

        profile_error = self._profile_preflight(candidate, profile_id)
        if profile_error is not None:
            if profile_error in IDENTITY_PROFILE_ERROR_CODES:
                service.fence_integrity(
                    candidate.id,
                    reason_code="identity_crosswire",
                    summary="The saved emulator identity no longer matches this Task.",
                    idempotency_key=f"runner:{candidate.id}:profile-integrity",
                )
                return 0
            return self._project_preflight_recovery(
                service,
                candidate,
                profile_id,
                profile_error,
                active,
                now,
            )

        wake = self._wake(candidate, task, active, now)
        if wake is None:
            return 0
        self.checkpoint("wake_claimed", candidate.id)

        if status == "replanning" and str(subtask.get("status")) == "user_takeover":
            try:
                released = self.operator.release_takeover(
                    principal_id=candidate.owner_principal_id,
                    controller_id=candidate.controller_id,
                    task_id=candidate.id,
                    action_id=action_id,
                )
            except Exception:
                self._project_recovery(
                    service, task, subtask, profile_id, wake,
                    reason_code="release_takeover_checkpoint_unavailable", now=now,
                    expected_current_status="replanning",
                )
                return 1
            if released.status is SettingsRunStatus.INTEGRITY_BLOCKED:
                self._project_result(service, task, subtask, profile_id, wake, released, now)
                return 1
            if released.status is not SettingsRunStatus.REPLANNING:
                self._project_recovery(
                    service, task, subtask, profile_id, wake,
                    reason_code="release_takeover_checkpoint_unconfirmed", now=now,
                    expected_current_status="replanning",
                )
                return 1
            service.upsert_subtask(
                candidate.id,
                kind=SETTINGS_SUBTASK_KIND,
                object_ref=profile_id,
                conversation_ref=None,
                status="replanning",
                priority=int(task["priority"]),
                current_stage="release_takeover_checkpoint",
            )
            task = service.inspect_task(candidate.id)
            if str(task["status"]) != "replanning":
                return self._settle_control_if_needed(task, wake)

        observed_status = str(task["status"])
        if observed_status != "running":
            try:
                task = service.transition_task(
                    candidate.id,
                    status="running",
                    reason_code="runner_dispatch",
                    summary="The resident Settings runner claimed this Task.",
                    recoverable=True,
                    idempotency_key=f"runner:{wake.wake_id}:running:{observed_status}",
                    expected_current_status=observed_status,
                )
            except TaskExpectedStatusConflict:
                current = service.inspect_task(candidate.id)
                return self._settle_control_if_needed(current, wake)
            self.checkpoint("running_committed", candidate.id)
        task = service.inspect_task(candidate.id)
        if str(task["status"]) != "running" or self._binding(task) != binding:
            return self._settle_control_if_needed(task, wake)

        if profile_error is not None:
            self._project_recovery(
                service, task, subtask, profile_id, wake,
                reason_code=profile_error, now=now,
                expected_current_status="running",
            )
            return 1

        # Last owner-scoped re-inspection immediately before the operator.  The
        # production kernel port repeats the canonical state/binding check at
        # capture and command-claim/dispatch boundaries.
        task = service.inspect_task(candidate.id)
        if str(task["status"]) != "running" or self._binding(task) != binding:
            return self._settle_control_if_needed(task, wake)
        self.checkpoint("before_operator", candidate.id)
        task = service.inspect_task(candidate.id)
        if str(task["status"]) != "running" or self._binding(task) != binding:
            return self._settle_control_if_needed(task, wake)
        try:
            result = self.operator.run(
                principal_id=candidate.owner_principal_id,
                controller_id=candidate.controller_id,
                task_id=candidate.id,
                action_id=action_id,
                profile_id=profile_id,
                idempotency_key=action_id,
            )
        except Exception:
            result = None

        self.checkpoint("operator_committed", candidate.id)

        after = service.inspect_task(candidate.id)
        if str(after["status"]) in _CONTROLLED or bool(after.get("terminal")):
            return self._settle_control_if_needed(after, wake)
        if result is None:
            self._project_recovery(
                service, after, subtask, profile_id, wake,
                reason_code="settings_runner_unavailable", now=now,
                expected_current_status="running",
            )
        else:
            self._project_result(service, after, subtask, profile_id, wake, result, now)
        return 1

    def _poll_general_task(self, candidate: Task, now: datetime) -> int:
        """Advance at most one durable general-UI step for one claimed wake."""

        admission = self._admission(
            candidate,
            runner_kind=GENERAL_RUNNER_KIND,
            runner_version=GENERAL_RUNNER_VERSION,
        )
        # A canonical row without the completed V2 alias/envelope saga has no
        # authority to touch Profile, wake, model, RuntimeKernel, or device.
        if admission is None or self.general_handler is None:
            return 0
        service = CanonicalTaskService(
            self.runtime_store,
            principal_id=candidate.owner_principal_id,
            controller_id=candidate.controller_id,
        )
        task = service.inspect_task(candidate.id)
        status = str(task["status"])
        active = self.coordination.active_wake(task_id=candidate.id)
        if bool(task.get("terminal")) or status == "cancelled":
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
            return 0

        binding = self._binding_for(task, GENERAL_SUBTASK_KIND)
        if (
            binding is None
            or admission.get("device_profile_id") != binding[1]
        ):
            service.fence_integrity(
                candidate.id,
                reason_code="identity_crosswire",
                summary="The general Android runner binding is not uniquely authorized.",
                idempotency_key=f"runner:{candidate.id}:general-binding-invalid",
            )
            return 0
        subtask, profile_id = binding

        if status in {"paused", "user_takeover"}:
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
            return 0
        if status not in _PREFLIGHT_STATUSES:
            return 0
        if status == "waiting_time":
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
                active = None
            due_at = task.get("next_wake_at")
            if not isinstance(due_at, str) or _parse_utc(due_at) > now:
                return 0

        profile_error = self._profile_preflight(candidate, profile_id)
        if profile_error is not None:
            if profile_error in IDENTITY_PROFILE_ERROR_CODES:
                service.fence_integrity(
                    candidate.id,
                    reason_code="identity_crosswire",
                    summary="The saved emulator identity no longer matches this Task.",
                    idempotency_key=f"runner:{candidate.id}:general-profile-integrity",
                )
                return 0
            return self._project_general_preflight_recovery(
                service, candidate, subtask, profile_id, profile_error, active, now,
            )

        wake = self._general_wake(candidate, task, active, now)
        if wake is None:
            return 0
        self.checkpoint("general_wake_claimed", candidate.id)

        observed_status = str(task["status"])
        if observed_status != "running":
            try:
                task = service.transition_task(
                    candidate.id,
                    status="running",
                    reason_code="android_ui_runner_dispatch",
                    summary="The resident Android UI runner claimed this Task.",
                    recoverable=True,
                    idempotency_key=f"runner:{wake.wake_id}:general-running:{observed_status}",
                    expected_current_status=observed_status,
                )
            except TaskExpectedStatusConflict:
                current = service.inspect_task(candidate.id)
                self.coordination.complete_wake(wake_id=wake.wake_id)
                return self._settle_control_if_needed(current, wake)

        # Accepted revisions become the current goal only at this explicit
        # action boundary.  No physical command is active when this hook runs.
        try:
            service.apply_revision_boundary(candidate.id)
        except Exception as error:
            _LOGGER.warning(
                "Android UI criteria freeze failed with %s",
                _safe_runner_error_code(error),
            )
            self._project_general_recovery(
                service, task, subtask, profile_id, wake,
                reason_code="revision_boundary_unavailable", now=now,
            )
            return 1
        task = service.inspect_task(candidate.id)
        binding = self._binding_for(task, GENERAL_SUBTASK_KIND)
        if (
            str(task["status"]) != "running"
            or binding is None
            or binding[1] != profile_id
            or self._admission(
                candidate,
                runner_kind=GENERAL_RUNNER_KIND,
                runner_version=GENERAL_RUNNER_VERSION,
            ) is None
        ):
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return self._settle_control_if_needed(task, wake)

        goal = self._general_goal(candidate.id)
        try:
            criteria = self.general_handler.criteria_for(
                task_id=candidate.id,
                goal=goal,
                revision=int(task["current_revision"]),
            )
        except Exception as error:
            _LOGGER.warning(
                "Android UI criteria unavailable: %s",
                _safe_runner_error_code(error),
            )
            self._project_general_recovery(
                service, task, subtask, profile_id, wake,
                reason_code="android_ui_criteria_unavailable", now=now,
            )
            return 1

        self.checkpoint("before_general_handler", candidate.id)
        task = service.inspect_task(candidate.id)
        if str(task["status"]) != "running" or self._binding_for(
            task, GENERAL_SUBTASK_KIND,
        ) != binding:
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return self._settle_control_if_needed(task, wake)
        try:
            result = self.general_handler.one_step(
                task_id=candidate.id, goal=goal, criteria=criteria,
            )
        except Exception as error:
            _LOGGER.warning(
                "Android UI one-step failed with %s",
                _safe_runner_error_code(error),
            )
            result = None
        self.checkpoint("general_step_committed", candidate.id)

        current = service.inspect_task(candidate.id)
        if str(current["status"]) in _CONTROLLED or bool(current.get("terminal")):
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return self._settle_control_if_needed(current, wake)
        if result is None:
            self._project_general_recovery(
                service, current, subtask, profile_id, wake,
                reason_code="android_ui_runner_unavailable",
                now=now,
            )
            return 1
        self._project_general_result(
            service, current, subtask, profile_id, wake, result, criteria, now,
        )
        return 1

    def _project_general_result(
        self,
        service: CanonicalTaskService,
        task: dict[str, Any],
        subtask: dict[str, Any],
        profile_id: str,
        wake: WakeDispatch,
        result: Any,
        criteria: Any,
        now: datetime,
    ) -> None:
        outcome = str(getattr(result, "outcome", "recoverable_unknown"))
        trusted = None
        if bool(getattr(result, "semantic_satisfied", False)):
            try:
                trusted = self.general_handler.trusted_terminal(
                    task_id=str(task["task_id"]), criteria=criteria,
                )
            except Exception:
                trusted = None
        expected_ref = getattr(result, "verification_ref", None)
        if (
            trusted is not None
            and getattr(trusted, "overall", None) == "satisfied"
            and getattr(trusted, "verification_ref", None) == expected_ref
        ):
            self._project_general_transition(
                service, task, profile_id, wake,
                subtask_status="succeeded",
                current_stage="semantic_goal_verified",
                target_status="succeeded",
                reason_code="android_ui_semantic_verified",
                summary="The frozen Android UI goal criteria were verified from fresh evidence.",
                recoverable=False,
                idempotency_key=f"runner:{wake.wake_id}:general-succeeded",
            )
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return

        if outcome == "replan":
            self._project_general_transition(
                service, task, profile_id, wake,
                subtask_status="replanning",
                current_stage="replanning",
                target_status="replanning",
                reason_code="android_ui_replanning",
                summary="The Android UI runner requires a fresh planning boundary.",
                idempotency_key=f"runner:{wake.wake_id}:general-replanning",
                recoverable=True,
                ordinary_failure=True,
            )
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return

        if outcome == "wait":
            seconds = getattr(result, "wait_seconds", None)
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= 10:
                self._project_general_recovery(
                    service, task, subtask, profile_id, wake,
                    reason_code="android_ui_wait_invalid", now=now,
                )
                return
            self._project_general_transition(
                service, task, profile_id, wake,
                subtask_status="waiting_time",
                current_stage="stabilization_wait",
                target_status="waiting_time",
                reason_code="android_ui_stabilization_wait",
                summary="The Android UI runner scheduled a short stabilization observation.",
                recoverable=True,
                next_wake_at=_format_utc(now + timedelta(seconds=float(seconds))),
                idempotency_key=f"runner:{wake.wake_id}:general-wait",
            )
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return

        if outcome == "terminal_unverified":
            # A zero-effect terminal proposal that K2 cannot verify is not
            # progress and must not become a timer-driven planning loop.  Keep
            # the Task recoverable, but require a real external event (for
            # example a revision, control, or device-state change) before the
            # scheduler is allowed to invoke the planner again.
            self._project_general_transition(
                service, task, profile_id, wake,
                subtask_status="waiting_event",
                current_stage="semantic_verification_unknown",
                target_status="waiting_event",
                reason_code="android_ui_terminal_unverified",
                summary="The Android UI terminal proposal was not verified; an external event is required before retry.",
                recoverable=True,
                idempotency_key=f"runner:{wake.wake_id}:general-terminal-unverified",
                ordinary_failure=True,
            )
            self.coordination.complete_wake(wake_id=wake.wake_id)
            return

        recoverable = outcome in {
            "after_pending_recovery", "after_evidence_unavailable",
            "decision_pending_recovery", "reconciled_effect_pending_evidence",
            "recoverable_unknown", "unresolved_effect",
        }
        if recoverable:
            self._project_general_recovery(
                service, task, subtask, profile_id, wake,
                reason_code=_safe_reason(outcome), now=now,
            )
            return

        self._project_general_transition(
            service, task, profile_id, wake,
            subtask_status="running",
            current_stage="fresh_observation_required",
            target_status="waiting_time",
            reason_code="android_ui_step_committed",
            summary="One bounded Android UI step completed; a fresh wake is required.",
            recoverable=True,
            next_wake_at=_format_utc(now + self.general_step_backoff),
            idempotency_key=f"runner:{wake.wake_id}:general-next-step",
        )
        self.coordination.complete_wake(wake_id=wake.wake_id)

    def _project_general_recovery(
        self,
        service: CanonicalTaskService,
        task: dict[str, Any],
        subtask: dict[str, Any],
        profile_id: str,
        wake: WakeDispatch,
        *,
        reason_code: str,
        now: datetime,
    ) -> None:
        self._project_general_transition(
            service, task, profile_id, wake,
            subtask_status="recovering",
            current_stage="recovering",
            target_status="waiting_time",
            reason_code=_safe_reason(reason_code),
            summary="The Android UI runner will retry after a recoverable runtime condition.",
            recoverable=True,
            idempotency_key=f"runner:{wake.wake_id}:general-recovering",
            next_wake_at=_format_utc(now + self.recovery_backoff),
            ordinary_failure=True,
        )
        self.coordination.complete_wake(wake_id=wake.wake_id)

    def _project_general_transition(
        self,
        service: CanonicalTaskService,
        task: dict[str, Any],
        profile_id: str,
        wake: WakeDispatch,
        *,
        subtask_status: str,
        current_stage: str,
        target_status: str,
        reason_code: str,
        summary: str,
        recoverable: bool,
        idempotency_key: str,
        next_wake_at: str | None = None,
        ordinary_failure: bool = False,
    ) -> bool:
        """Commit a general result only while its canonical authority is current."""

        task_id = str(task["task_id"])
        self.checkpoint("before_general_projection", task_id)
        try:
            service.project_runner_result(
                task_id,
                expected_revision=int(task["current_revision"]),
                expected_current_status="running",
                kind=GENERAL_SUBTASK_KIND,
                object_ref=profile_id,
                conversation_ref=None,
                subtask_status=subtask_status,
                current_stage=current_stage,
                target_status=target_status,
                reason_code=reason_code,
                summary=summary,
                recoverable=recoverable,
                idempotency_key=idempotency_key,
                next_wake_at=next_wake_at,
                ordinary_failure=ordinary_failure,
            )
        except TaskExpectedStatusConflict:
            # A control or revision committed after the handler result was
            # produced.  Discard the late projection and let the newer
            # canonical authority decide the next wake.
            return False
        return True

    def _project_general_preflight_recovery(
        self,
        service: CanonicalTaskService,
        candidate: Task,
        subtask: dict[str, Any],
        profile_id: str,
        reason_code: str,
        active: WakeDispatch | None,
        now: datetime,
    ) -> int:
        current = service.inspect_task(candidate.id)
        status = str(current["status"])
        if status in _CONTROLLED or bool(current.get("terminal")):
            return 0
        attempt = f"runner:{candidate.id}:general-profile:{current['updated_at']}"
        if status != "running":
            try:
                current = service.transition_task(
                    candidate.id, status="running", reason_code="profile_preflight",
                    summary="The Android UI runner is checking the saved emulator profile.",
                    recoverable=True, idempotency_key=f"{attempt}:running",
                    expected_current_status=status,
                )
            except TaskExpectedStatusConflict:
                return 0
        self._project_general_recovery(
            service, current, subtask, profile_id,
            active or WakeDispatch(
                wake_id=f"{attempt}:synthetic", task_id=candidate.id,
                task_revision=int(current["current_revision"]),
                due_at=current["updated_at"], state="active",
            ),
            reason_code=reason_code, now=now,
        )
        if active is not None:
            self.coordination.complete_wake(wake_id=active.wake_id)
        return 1

    def _general_wake(
        self,
        candidate: Task,
        task: dict[str, Any],
        active: WakeDispatch | None,
        now: datetime,
    ) -> WakeDispatch | None:
        status = str(task["status"])
        if status == "scheduled":
            wake = WakeDispatch(
                wake_id=general_initial_wake_id(candidate.id), task_id=candidate.id,
                task_revision=int(task["current_revision"]), due_at=candidate.created_at,
                state="active",
            )
        elif status == "waiting_time":
            due_at = task.get("next_wake_at")
            if not isinstance(due_at, str) or _parse_utc(due_at) > now:
                return None
            wake = WakeDispatch(
                wake_id=stable_wake_id(candidate.id, int(task["current_revision"]), due_at),
                task_id=candidate.id, task_revision=int(task["current_revision"]),
                due_at=due_at, state="active",
            )
        elif status in _RESUMABLE:
            if active is not None and self.coordination.resume_active_wake(active):
                return active
            updated_at = str(task["updated_at"])
            digest = hashlib.sha256(
                f"{candidate.id}\x00{task['current_revision']}\x00{updated_at}\x00{GENERAL_RUNNER_KIND}".encode()
            ).hexdigest()
            wake = WakeDispatch(
                wake_id=f"task:{candidate.id}:general-resume:{digest}",
                task_id=candidate.id, task_revision=int(task["current_revision"]),
                due_at=updated_at, state="active",
            )
        else:
            return None
        outcome = self.coordination.claim_wake(wake, overlap_policy=OverlapPolicy.SKIP)
        if outcome == "dispatch" or (
            outcome == "duplicate" and self.coordination.resume_active_wake(wake)
        ):
            return wake
        return None

    def _admission(
        self, candidate: Task, *, runner_kind: str, runner_version: str,
    ) -> dict[str, Any] | None:
        lookup = getattr(self.runner_admission, "runner_admission", None)
        if not callable(lookup):
            return None
        try:
            fact = lookup(
                candidate.id,
                principal_id=candidate.owner_principal_id,
                controller_id=candidate.controller_id,
            )
        except Exception:
            return None
        if not isinstance(fact, dict):
            return None
        expected = {
            "task_id": candidate.id,
            "principal_id": candidate.owner_principal_id,
            "controller_id": candidate.controller_id,
            "runner_kind": runner_kind,
            "runner_version": runner_version,
        }
        if any(fact.get(key) != value for key, value in expected.items()):
            return None
        profile_id = fact.get("device_profile_id")
        if not isinstance(profile_id, str) or not profile_id.strip():
            return None
        return fact

    def _general_goal(self, task_id: str) -> str:
        goal = str(self.runtime_store.get_session(task_id).original_instruction)
        for revision in self.runtime_store.task_revisions(task_id):
            if getattr(revision.status, "value", revision.status) != "applied":
                continue
            kind = str(getattr(revision.kind, "value", revision.kind))
            if kind == "revise":
                goal = revision.instruction
            elif kind == "add":
                goal = f"{goal}\n{revision.instruction}"
        return goal

    def _wake(
        self,
        candidate: Task,
        task: dict[str, Any],
        active: WakeDispatch | None,
        now: datetime,
    ) -> WakeDispatch | None:
        status = str(task["status"])
        if status == "scheduled":
            wake = WakeDispatch(
                wake_id=initial_wake_id(candidate.id),
                task_id=candidate.id,
                task_revision=int(task["current_revision"]),
                due_at=candidate.created_at,
                state="active",
            )
            outcome = self.coordination.claim_wake(wake, overlap_policy=OverlapPolicy.SKIP)
            if outcome == "dispatch" or (
                outcome == "duplicate" and self.coordination.resume_active_wake(wake)
            ):
                return wake
            return None
        if status == "waiting_time":
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
            due_at = task.get("next_wake_at")
            if not isinstance(due_at, str) or _parse_utc(due_at) > now:
                return None
            wake = WakeDispatch(
                wake_id=stable_wake_id(candidate.id, int(task["current_revision"]), due_at),
                task_id=candidate.id,
                task_revision=int(task["current_revision"]),
                due_at=due_at,
                state="active",
            )
            return wake if self.coordination.claim_wake(
                wake, overlap_policy=OverlapPolicy.SKIP
            ) == "dispatch" else None
        if status in _RESUMABLE and active is not None and self.coordination.resume_active_wake(active):
            return active
        return None

    def _project_result(
        self,
        service: CanonicalTaskService,
        task: dict[str, Any],
        subtask: dict[str, Any],
        profile_id: str,
        wake: WakeDispatch,
        result: EmulatorSettingsRun,
        now: datetime,
    ) -> None:
        status = result.status
        if status is SettingsRunStatus.SUCCEEDED:
            subtask_status, stage = "succeeded", "verified_settings_foreground"
        elif status is SettingsRunStatus.REPLANNING:
            subtask_status, stage = "replanning", "replanning"
        elif status is SettingsRunStatus.USER_TAKEOVER:
            subtask_status, stage = "user_takeover", "user_takeover"
        elif status is SettingsRunStatus.INTEGRITY_BLOCKED:
            subtask_status, stage = "integrity_blocked", "integrity_blocked"
        else:
            subtask_status, stage = "recovering", "recovering"
        service.upsert_subtask(
            str(task["task_id"]),
            kind=SETTINGS_SUBTASK_KIND,
            object_ref=profile_id,
            conversation_ref=None,
            status=subtask_status,
            priority=int(task["priority"]),
            current_stage=stage,
        )
        current = service.inspect_task(str(task["task_id"]))
        if str(current["status"]) in _CONTROLLED or bool(current.get("terminal")):
            self._settle_control_if_needed(current, wake)
            return
        if status is SettingsRunStatus.SUCCEEDED:
            try:
                service.transition_task(
                    str(task["task_id"]),
                    status="succeeded",
                    reason_code="settings_verified",
                    summary="Android system Settings was verified in a fresh observation.",
                    recoverable=False,
                    idempotency_key=f"runner:{wake.wake_id}:succeeded",
                    expected_current_status="running",
                )
            except TaskExpectedStatusConflict:
                current = service.inspect_task(str(task["task_id"]))
                self._settle_control_if_needed(current, wake)
                return
            self.coordination.complete_wake(wake_id=wake.wake_id)
        elif status is SettingsRunStatus.REPLANNING:
            self._ordinary_failure_if_expected(
                service,
                task_id=str(task["task_id"]),
                wake=wake,
                reason_code="settings_replanning",
                summary="The Settings runner requires a fresh planning boundary.",
                idempotency_key=f"runner:{wake.wake_id}:replanning",
                expected_current_status="running",
                replan=True,
            )
        elif status is SettingsRunStatus.USER_TAKEOVER:
            try:
                service.transition_task(
                    str(task["task_id"]),
                    status="user_takeover",
                    reason_code="user_takeover",
                    summary="The user has taken control of the emulator.",
                    recoverable=True,
                    idempotency_key=f"runner:{wake.wake_id}:user-takeover",
                    expected_current_status="running",
                )
            except TaskExpectedStatusConflict:
                current = service.inspect_task(str(task["task_id"]))
                self._settle_control_if_needed(current, wake)
                return
        elif status is SettingsRunStatus.INTEGRITY_BLOCKED:
            reason = str(result.reason_code or "")
            canonical_reason = (
                "identity_crosswire"
                if reason in _OPERATOR_IDENTITY_ERROR_CODES
                else "fabricated_success"
            )
            service.fence_integrity(
                str(task["task_id"]),
                reason_code=canonical_reason,
                summary="The Settings execution integrity check failed closed.",
                idempotency_key=f"runner:{wake.wake_id}:integrity",
            )
            self.coordination.complete_wake(wake_id=wake.wake_id)
        else:
            self._project_recovery(
                service, current, subtask, profile_id, wake,
                reason_code=_safe_reason(result.reason_code), now=now,
                update_subtask=False, expected_current_status="running",
            )

    def _project_recovery(
        self,
        service: CanonicalTaskService,
        task: dict[str, Any],
        subtask: dict[str, Any],
        profile_id: str,
        wake: WakeDispatch,
        *,
        reason_code: str,
        now: datetime,
        expected_current_status: str,
        update_subtask: bool = True,
    ) -> None:
        if update_subtask:
            service.upsert_subtask(
                str(task["task_id"]),
                kind=SETTINGS_SUBTASK_KIND,
                object_ref=profile_id,
                conversation_ref=None,
                status="recovering",
                priority=int(task["priority"]),
                current_stage="recovering",
            )
        current = service.inspect_task(str(task["task_id"]))
        if str(current["status"]) in _CONTROLLED or bool(current.get("terminal")):
            self._settle_control_if_needed(current, wake)
            return
        if self._ordinary_failure_if_expected(
            service,
            task_id=str(task["task_id"]),
            wake=wake,
            reason_code=_safe_reason(reason_code),
            summary="The Settings runner will retry after a recoverable runtime condition.",
            idempotency_key=f"runner:{wake.wake_id}:recovering",
            expected_current_status=expected_current_status,
            next_wake_at=_format_utc(now + self.recovery_backoff),
        ):
            self.coordination.complete_wake(wake_id=wake.wake_id)

    def _ordinary_failure_if_expected(
        self,
        service: CanonicalTaskService,
        *,
        task_id: str,
        wake: WakeDispatch | None,
        reason_code: str,
        summary: str,
        idempotency_key: str,
        expected_current_status: str,
        replan: bool = False,
        next_wake_at: str | None = None,
    ) -> bool:
        self.checkpoint("before_ordinary_failure", task_id)
        try:
            service.ordinary_failure(
                task_id,
                reason_code=reason_code,
                summary=summary,
                idempotency_key=idempotency_key,
                replan=replan,
                next_wake_at=next_wake_at,
                expected_current_status=expected_current_status,
            )
        except TaskExpectedStatusConflict:
            current = service.inspect_task(task_id)
            if wake is not None:
                self._settle_control_if_needed(current, wake)
            return False
        return True

    def _settle_control_if_needed(self, task: dict[str, Any], wake: WakeDispatch) -> int:
        if bool(task.get("terminal")) or str(task.get("status")) == "cancelled":
            self.coordination.complete_wake(wake_id=wake.wake_id)
        return 0

    def _profile_preflight(self, candidate: Task, profile_id: str) -> str | None:
        try:
            self.profiles.resolve_ready(
                principal_id=candidate.owner_principal_id,
                controller_id=candidate.controller_id,
                profile_id=profile_id,
            )
        except EmulatorProfileServiceError as error:
            return _safe_reason(error.code)
        except Exception:
            return "emulator_profile_unavailable"
        return None

    def _project_preflight_recovery(
        self,
        service: CanonicalTaskService,
        candidate: Task,
        profile_id: str,
        reason_code: str,
        active: WakeDispatch | None,
        now: datetime,
    ) -> int:
        """Record a safe retry without claiming a wake or calling the operator.

        A scheduled Task cannot enter ``waiting_time`` directly, so this makes
        the smallest owner-scoped scheduled -> running -> ordinary-failure
        transition.  Both writes use compare-and-swap status checks: a user
        control that wins either gap remains authoritative.  The attempt key
        is durable across a crash but changes with each new due time.
        """

        current = service.inspect_task(candidate.id)
        status = str(current["status"])
        if status in _CONTROLLED or bool(current.get("terminal")):
            return 0
        if status not in _PREFLIGHT_STATUSES:
            return 0

        attempt_key = self._preflight_attempt_key(candidate, current)
        if status != "running":
            try:
                current = service.transition_task(
                    candidate.id,
                    status="running",
                    reason_code="profile_preflight",
                    summary="The Settings runner is checking the saved emulator profile.",
                    recoverable=True,
                    idempotency_key=f"{attempt_key}:running",
                    expected_current_status=status,
                )
            except TaskExpectedStatusConflict:
                return 0
        if str(current["status"]) != "running":
            return 0
        if self._ordinary_failure_if_expected(
            service,
            task_id=candidate.id,
            wake=active,
            reason_code=reason_code,
            summary="The saved emulator is temporarily unavailable; the Settings runner will retry.",
            idempotency_key=f"{attempt_key}:recovering",
            expected_current_status="running",
            next_wake_at=_format_utc(now + self.recovery_backoff),
        ):
            if active is not None:
                self.coordination.complete_wake(wake_id=active.wake_id)
            return 1
        return 0

    @staticmethod
    def _preflight_attempt_key(candidate: Task, task: dict[str, Any]) -> str:
        status = str(task["status"])
        if status == "scheduled":
            return f"runner:{initial_wake_id(candidate.id)}:profile-preflight"
        due_at = task.get("next_wake_at")
        if isinstance(due_at, str):
            return (
                f"runner:{stable_wake_id(candidate.id, int(task['current_revision']), due_at)}"
                ":profile-preflight"
            )
        return (
            f"runner:{candidate.id}:profile-preflight:{status}:"
            f"{task['updated_at']}"
        )

    @staticmethod
    def _has_exact_marker(task: Task) -> bool:
        return (
            task.origin.get("runner_kind") == RUNNER_KIND
            and task.origin.get("runner_version") == RUNNER_VERSION
        )

    @staticmethod
    def _has_exact_general_marker(task: Task) -> bool:
        return (
            task.origin.get("runner_kind") == GENERAL_RUNNER_KIND
            and task.origin.get("runner_version") == GENERAL_RUNNER_VERSION
        )

    @staticmethod
    def _binding(task: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        return ResidentV2TaskScheduler._binding_for(task, SETTINGS_SUBTASK_KIND)

    @staticmethod
    def _binding_for(
        task: dict[str, Any], kind: str,
    ) -> tuple[dict[str, Any], str] | None:
        subtasks = task.get("subtasks")
        if not isinstance(subtasks, list):
            return None
        settings = [
            item for item in subtasks
            if isinstance(item, dict) and item.get("kind") == kind
        ]
        if len(settings) != 1:
            return None
        profile_id = settings[0].get("object_ref")
        if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 512:
            return None
        return settings[0], profile_id

    def _lease(self, now: datetime) -> bool:
        return self.coordination.acquire_lease(
            owner_id=self.owner_id,
            now=now,
            ttl=self.lease_ttl,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("resident scheduler clock must be timezone-aware")
        return value.astimezone(UTC)


def _safe_reason(value: Any) -> str:
    raw = str(value or "settings_runner_recovering")
    safe = "".join(
        character if character.isascii() and (character.isalnum() or character in "_-") else "_"
        for character in raw
    )[:128].strip("_")
    return safe or "settings_runner_recovering"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("runner wake timestamp must include timezone")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "GENERAL_RUNNER_KIND",
    "GENERAL_RUNNER_VERSION",
    "RUNNER_KIND",
    "RUNNER_VERSION",
    "ResidentV2TaskScheduler",
    "general_initial_wake_id",
    "initial_wake_id",
    "settings_action_identity",
]
