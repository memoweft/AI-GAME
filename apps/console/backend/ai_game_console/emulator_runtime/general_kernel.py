"""Pure generic-command bridge used by the future Android UI runner.

This is intentionally composition-free: it accepts a fake dispatcher in unit
tests and never discovers devices, starts a scheduler, or calls ADB itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

from ..agent_runtime.domain import RunnerDispatchCommit, RunnerDispatchRequest
from ..agent_runtime.service import CanonicalTaskService
from .general_store import (
    GenericCommandConflict,
    GenericCommandClaim,
    SQLiteGenericCommandStore,
    command_payload_digest,
    require_supported_command,
)


@dataclass(frozen=True, slots=True)
class GenericDispatchResult:
    accepted: bool
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome not in {"accepted", "rejected"}:
            raise ValueError("generic dispatch result outcome is invalid")
        if self.accepted != (self.outcome == "accepted"):
            raise ValueError("generic dispatch accepted/outcome disagree")


class GenericPhysicalDispatcher(Protocol):
    def dispatch(
        self, *, canonical_device_id: str, command_type: str, payload: Mapping[str, object],
        command_id: str, binding: GenericProfileBinding, claim: GenericCommandClaim,
    ) -> GenericDispatchResult: ...


@dataclass(frozen=True, slots=True)
class GenericProfileBinding:
    """Authoritative immutable device binding for one physical invocation."""

    owner_principal_id: str
    controller_id: str
    profile_id: str
    profile_generation: int
    device_boot_id: str
    canonical_device_id: str


class GenericProfileResolver(Protocol):
    def resolve_ready(
        self, *, principal_id: str, controller_id: str, profile_id: str,
        expected_generation: int | None = None,
    ) -> object: ...


class GenericEvidenceLookup(Protocol):
    """Resolve a fresh command-correlated observation from authoritative storage."""

    def resolve_reconciliation(
        self, *, command_id: str, observation_id: str, claim: GenericCommandClaim,
    ) -> "GenericReconciliationEvidence": ...


@dataclass(frozen=True, slots=True)
class GenericReconciliationEvidence:
    """Fresh, exact-bound evidence required to release an unknown effect."""

    task_id: str
    dispatch_id: str
    step_id: str
    subtask_id: str
    action_id: str
    owner_principal_id: str
    controller_id: str
    profile_id: str
    profile_generation: int
    device_boot_id: str
    canonical_device_id: str
    observation_id: str
    evidence_refs: tuple[str, ...]
    command_id: str
    captured_at: str
    persisted_at: str
    observed_effect: bool

    def __post_init__(self) -> None:
        identities = (
            self.task_id, self.dispatch_id, self.step_id, self.subtask_id,
            self.action_id, self.observation_id, self.command_id,
        )
        if any(not item for item in identities) or not self.evidence_refs:
            raise ValueError("reconciliation requires fresh observation and evidence")
        datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        datetime.fromisoformat(self.persisted_at.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class GenericDispatchAttempt:
    commit: RunnerDispatchCommit
    claim: GenericCommandClaim
    physically_dispatched: bool


class GenericKernelCommandPort:
    """Enforce commit -> claim -> one physical call -> settle ordering."""

    def __init__(
        self,
        *,
        tasks: CanonicalTaskService,
        claims: SQLiteGenericCommandStore,
        dispatcher: GenericPhysicalDispatcher,
        profiles: GenericProfileResolver | None = None,
        evidence_lookup: GenericEvidenceLookup | None = None,
    ) -> None:
        self._tasks = tasks
        self._claims = claims
        self._dispatcher = dispatcher
        self._profiles = profiles
        self._evidence_lookup = evidence_lookup
        self._claims.initialize()

    def dispatch_once(
        self,
        *,
        task_id: str,
        request: RunnerDispatchRequest,
        payload: Mapping[str, object],
    ) -> GenericDispatchAttempt:
        """Perform at most one physical invocation for a durable command ID.

        A replay only returns the persisted claim.  It cannot retry an
        outcome-unknown command or create a replacement action under the same
        canonical Task; a future planner must first reconcile and cross an
        explicit replan boundary.
        """

        # WAIT/SCREENSHOT/CAPTURE_SNAPSHOT/INPUT_TEXT_UNICODE are declared by
        # old Kernel enums but have no generic executor+verification contract
        # yet.  Reject before canonical commit, command claim, lease or device.
        require_supported_command(request.command_type)
        if command_payload_digest(request.command_type, payload) != request.payload_digest:
            raise ValueError("runner command payload does not match its redacted digest")
        commit = self._tasks.commit_runner_dispatch(task_id, request=request)
        claim, created = self._claims.claim(
            dispatch_id=commit.dispatch_id,
            task_id=commit.task_id,
            step_id=request.step_id,
            subtask_id=request.subtask_id,
            action_id=request.action_id,
            command_id=request.command_id,
            canonical_device_id=request.canonical_device_id,
            owner_principal_id=commit.owner_principal_id,
            controller_id=commit.controller_id,
            profile_id=request.profile_id,
            profile_generation=request.profile_generation,
            device_boot_id=request.device_boot_id,
            command_type=request.command_type,
            payload_digest=request.payload_digest,
        )
        if not created:
            claim = self._claims.recover_unfinished(request.command_id)
            return GenericDispatchAttempt(commit=commit, claim=claim, physically_dispatched=False)
        try:
            binding = self._resolve_exact_binding(commit)
        except Exception:
            # No physical call has happened.  Preserve the committed boundary
            # but make this command terminally non-dispatchable; a later plan
            # must use a new intent after a fresh observation.
            claim = self._claims.settle(request.command_id, outcome="rejected")
            self._tasks.settle_runner_effect(
                task_id, command_id=request.command_id, outcome="preflight_rejected",
            )
            return GenericDispatchAttempt(commit=commit, claim=claim, physically_dispatched=False)
        claim = self._claims.begin_dispatch(request.command_id)
        try:
            result = self._dispatcher.dispatch(
                canonical_device_id=request.canonical_device_id,
                command_type=request.command_type,
                payload=payload,
                command_id=request.command_id,
                binding=binding,
                claim=claim,
            )
        except Exception:
            claim = self._claims.mark_outcome_unknown(request.command_id)
            return GenericDispatchAttempt(commit=commit, claim=claim, physically_dispatched=True)
        claim = self._claims.settle(request.command_id, outcome=result.outcome)
        return GenericDispatchAttempt(commit=commit, claim=claim, physically_dispatched=True)

    def reconcile(
        self, *, command_id: str,
        observation_id: str | None = None,
        evidence: GenericReconciliationEvidence | None = None,
        reliably_happened: bool | None = None,
    ) -> GenericCommandClaim:
        """Resolve an unknown command only with fresh exact-bound evidence.

        ``reliably_happened`` is retained only as a source-compatible spelling
        for callers, but a bare boolean is deliberately rejected: it cannot
        establish task/profile/device ownership or causal observation.
        """

        if observation_id is None and evidence is not None:
            observation_id = evidence.observation_id
        if observation_id is None or self._evidence_lookup is None:
            raise GenericCommandConflict("reconciliation requires exact fresh evidence")
        # Caller input supplies only an opaque observation handle.  The
        # verdict, owner/device binding, artifact refs and freshness are read
        # back from a separately injected authority; never trust planner text.
        claim = self._claims.get(command_id)
        if claim is None:
            raise GenericCommandConflict("command claim was not found")
        evidence = self._evidence_lookup.resolve_reconciliation(
            command_id=command_id, observation_id=observation_id, claim=claim,
        )
        if (
            evidence.task_id != claim.task_id
            or evidence.dispatch_id != claim.dispatch_id
            or evidence.step_id != claim.step_id
            or evidence.subtask_id != claim.subtask_id
            or evidence.action_id != claim.action_id
            or evidence.owner_principal_id != claim.owner_principal_id
            or evidence.controller_id != claim.controller_id
            or evidence.profile_id != claim.profile_id
            or evidence.profile_generation != claim.profile_generation
            or evidence.device_boot_id != claim.device_boot_id
            or evidence.canonical_device_id != claim.canonical_device_id
        ):
            raise GenericCommandConflict("reconciliation evidence binding does not match command")
        if evidence.command_id != command_id:
            raise GenericCommandConflict("reconciliation evidence does not belong to command")
        causal_boundary = claim.settled_at or claim.dispatch_started_at or claim.claimed_at
        if (
            _as_utc(evidence.captured_at) <= _as_utc(causal_boundary)
            or _as_utc(evidence.persisted_at) <= _as_utc(causal_boundary)
        ):
            raise GenericCommandConflict(
                "reconciliation observation is not captured and persisted after command receipt"
            )
        # An owner-scoped canonical inspection is intentionally performed even
        # though no mutation occurs: a stale foreign service cannot settle an
        # effect that belongs to another owner after process recovery.
        task = self._tasks.inspect_task(claim.task_id)
        if (
            task.get("task_id") != claim.task_id
            or task.get("current_subtask_id") != claim.subtask_id
        ):
            raise GenericCommandConflict("reconciliation task no longer matches command")
        canonical_task = self._tasks.store.get_task(claim.task_id)
        origin = dict(canonical_task.origin)
        dispatches = self._tasks.store.runner_dispatches(claim.task_id)
        dispatch = next(
            (item for item in dispatches if item.dispatch_id == claim.dispatch_id), None,
        )
        if (
            dispatch is None
            or dispatch.request.command_id != claim.command_id
            or dispatch.request.action_id != claim.action_id
            or dispatch.request.step_id != claim.step_id
            or dispatch.request.subtask_id != claim.subtask_id
            or origin.get("runner_kind") != dispatch.request.runner_kind
            or str(origin.get("runner_version")) != dispatch.request.runner_version
        ):
            raise GenericCommandConflict("reconciliation canonical effect binding changed")
        if evidence.observed_effect:
            settled = (
                claim
                if claim.state == "SETTLED" and claim.outcome == "accepted"
                else self._claims.settle(command_id, outcome="reconciled_happened")
            )
            self._tasks.settle_runner_effect(
                claim.task_id, command_id=command_id, outcome="observed",
            )
            return settled
        settled = (
            claim
            if claim.state == "SETTLED" and claim.outcome == "accepted"
            else self._claims.require_replan_after_non_effect(command_id)
        )
        self._tasks.settle_runner_effect(
            claim.task_id, command_id=command_id, outcome="not_observed",
        )
        return settled

    def _resolve_exact_binding(self, commit: RunnerDispatchCommit) -> GenericProfileBinding:
        if self._profiles is None:
            raise GenericCommandConflict("authoritative profile resolver is required")
        request = commit.request
        profile = self._profiles.resolve_ready(
            principal_id=commit.owner_principal_id,
            controller_id=commit.controller_id,
            profile_id=request.profile_id,
            expected_generation=request.profile_generation,
        )
        binding = GenericProfileBinding(
            owner_principal_id=getattr(profile, "owner_principal_id"),
            controller_id=getattr(profile, "owner_controller_id"),
            profile_id=getattr(profile, "profile_id"),
            profile_generation=getattr(profile, "profile_generation"),
            device_boot_id=getattr(profile, "boot_id"),
            canonical_device_id=getattr(profile, "canonical_device_id"),
        )
        expected = GenericProfileBinding(
            owner_principal_id=commit.owner_principal_id,
            controller_id=commit.controller_id,
            profile_id=request.profile_id,
            profile_generation=request.profile_generation,
            device_boot_id=request.device_boot_id,
            canonical_device_id=request.canonical_device_id,
        )
        if binding != expected:
            raise GenericCommandConflict("authoritative profile binding no longer matches dispatch")
        return binding


def _as_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
