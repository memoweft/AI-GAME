"""Package H composition adapters for the WeftMate execution v2 surface.

The classes in this module deliberately contain no process or device lifecycle.
They adapt the authoritative Package B/D/E stores and accept explicit discovery,
observation and artifact ports so application startup stays fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from typing import Any, Callable, Iterable

from .agent_runtime.domain import TaskControlAction
from .agent_runtime.service import CanonicalTaskService
from .execution_contract.service import ExecutionContractError
from .user_fact_runtime import NeedUserFactStatus


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _jsonable(item: Any) -> Any:
    if is_dataclass(item):
        return asdict(item)
    value = getattr(item, "value", None)
    return value if value is not None else item


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left), str(right))


class CanonicalTaskPortAdapter:
    """Map A's v2 port to the one canonical AgentSession Task aggregate."""

    def __init__(
        self,
        *,
        runtime_store: Any,
        execution_store: Any,
        fact_questions: Any | None = None,
        profiles: Any | None = None,
        device_projection: Callable[[str, str, str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.runtime_store = runtime_store
        self.execution_store = execution_store
        self.fact_questions = fact_questions
        self.profiles = profiles
        self.device_projection = device_projection

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        principal_id = str(request["principal_id"])
        controller_id = str(request["controller_id"])
        service = CanonicalTaskService(
            self.runtime_store,
            principal_id=principal_id,
            controller_id=controller_id,
        )
        goal = request.get("goal") or {}
        summary = str(goal.get("summary") or "").strip()
        if not summary:
            raise ValueError("goal summary is required")
        requested_profile_id = request.get("device_profile_id")
        runner_kind = request.get("runner_kind")
        runner_version = request.get("runner_version")
        if runner_kind not in {None, "android_ui_agent"} or (
            runner_kind is not None and runner_version != "1"
        ) or (runner_kind is None and runner_version is not None):
            raise ExecutionContractError(
                "CAPABILITY_UNAVAILABLE", "The requested phone capability is unavailable.", 409
            )
        if runner_kind == "android_ui_agent" and requested_profile_id is None:
            raise ExecutionContractError(
                "DEVICE_PROFILE_REQUIRED",
                "The Android emulator runner requires a saved device profile.",
                409,
            )
        if requested_profile_id is not None:
            if self.profiles is None:
                raise ExecutionContractError(
                    "DEVICE_PROFILE_UNAVAILABLE", "Device profiles are unavailable.", 503
                )
            self.profiles.require_profile(
                principal_id=principal_id,
                controller_id=controller_id,
                profile_id=str(requested_profile_id),
            )
        origin = {
            key: request.get("origin", {}).get(key)
            for key in (
                "dsh_session_id", "created_execution_id", "created_turn_id",
                "created_tool_call_id", "root_call_id",
            )
            if request.get("origin", {}).get(key) is not None
        }
        if runner_kind == "android_ui_agent":
            origin.update({
                "runner_kind": runner_kind,
                "runner_version": "1",
            })
        created = service.create_task(summary, str(request["idempotency_key"]), origin=origin)
        priority = int(request.get("priority", 50))
        if priority != int(created.get("priority", 50)):
            service.control_task(
                str(created["task_id"]),
                action=TaskControlAction.REPRIORITIZE.value,
                idempotency_key=f"{request['idempotency_key']}:priority",
                expected_revision=int(created["current_revision"]),
                requested_by={"source": "weftmate-v2-create"},
                priority=priority,
            )
        if runner_kind == "android_ui_agent":
            service.upsert_subtask(
                str(created["task_id"]),
                kind=str(runner_kind),
                object_ref=str(requested_profile_id),
                conversation_ref=None,
                status="scheduled",
                priority=priority,
                current_stage="fresh_before_observation",
            )
        return self._project(str(created["task_id"]), principal_id, controller_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        owner = self._canonical_owner(task_id)
        if owner is None:
            return None
        return self._project(task_id, *owner)

    def list(self, request: dict[str, Any]) -> dict[str, Any]:
        allowed = [str(value) for value in request.get("task_ids", [])]
        status = request.get("status")
        offset = self._cursor(request.get("cursor"))
        items: list[dict[str, Any]] = []
        for task_id in allowed:
            projection = self.get(task_id)
            if projection is None or projection.get("archived") is True:
                continue
            if status is None or projection["status"] == status:
                items.append(projection)
        items.sort(key=lambda item: (item["timestamps"]["updated_at"], item["task_id"]), reverse=True)
        limit = int(request.get("limit", 100))
        page = items[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "items": page,
            "next_cursor": str(next_offset) if next_offset < len(items) else None,
        }

    def events(self, task_id: str, *, after: int, limit: int) -> dict[str, Any]:
        owner = self._required_owner(task_id)
        service = CanonicalTaskService(
            self.runtime_store, principal_id=owner[0], controller_id=owner[1]
        )
        source = service.events(task_id, after=after, limit=limit)
        items = []
        for event in source:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            items.append({
                "schema_version": 2,
                "event_id": str(event["event_id"]),
                "task_id": task_id,
                "cursor": int(event["cursor"]),
                "type": str(event["type"]),
                "occurred_at": str(event["occurred_at"]),
                "summary": str(data.get("summary") or "")[:1500],
                "reason_code": data.get("reason_code"),
            })
        return {
            "items": items,
            "next_cursor": max((item["cursor"] for item in items), default=after),
        }

    def revise(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        service = self._service_for_request(task_id, request)
        service.revise_task(
            task_id,
            base_revision=int(request["base_revision"]),
            kind=str(request["kind"]),
            instruction=str(request["instruction"]),
            patch={},
            idempotency_key=str(request["idempotency_key"]),
            requested_by={"source": "weftmate-v2"},
            effective_boundary=str(request.get("effective_boundary") or "after_current_action"),
        )
        return self._project(task_id, str(request["principal_id"]), str(request["controller_id"]))

    def control(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        service = self._service_for_request(task_id, request)
        current = service.inspect_task(task_id)
        expected = request.get("expected_revision")
        service.control_task(
            task_id,
            action=str(request["action"]),
            idempotency_key=str(request["idempotency_key"]),
            expected_revision=int(current["current_revision"] if expected is None else expected),
            requested_by={"source": "weftmate-v2"},
        )
        return self._project(task_id, str(request["principal_id"]), str(request["controller_id"]))

    def answer(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._service_for_request(task_id, request)
        if self.fact_questions is None:
            raise ExecutionContractError(
                "TASK_QUESTION_UNAVAILABLE", "The pending question is unavailable.", 503
            )
        need_id = str(request["question_id"])
        try:
            need = self.fact_questions.facts.store.get_need(need_id)
        except Exception:
            raise ExecutionContractError("TASK_QUESTION_NOT_FOUND", "The pending question was not found.", 404) from None
        if not _same(str(need.session_id), task_id) or str(_value(need.status, "value", need.status)) != NeedUserFactStatus.OPEN.value:
            raise ExecutionContractError("TASK_QUESTION_NOT_FOUND", "The pending question was not found.", 404)
        self.fact_questions.answer(
            need_id=need_id,
            value=request.get("value"),
            idempotency_key=str(request["idempotency_key"]),
            provenance={"source": "weftmate-dsh-v2"},
        )
        return self._project(task_id, str(request["principal_id"]), str(request["controller_id"]))

    def archive(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        service = self._service_for_request(task_id, request)
        current = service.inspect_task(task_id)
        service.control_task(
            task_id,
            action=TaskControlAction.ARCHIVE.value,
            idempotency_key=str(request["idempotency_key"]),
            expected_revision=int(current["current_revision"]),
            requested_by={"source": "weftmate-v2"},
        )
        return self._project(task_id, str(request["principal_id"]), str(request["controller_id"]))

    def _project(self, task_id: str, principal_id: str, controller_id: str) -> dict[str, Any]:
        service = CanonicalTaskService(
            self.runtime_store, principal_id=principal_id, controller_id=controller_id
        )
        task = service.inspect_task(task_id)
        session = self.runtime_store.get_session(task_id)
        subtasks = list(task.get("subtasks") or [])
        current_subtask = next(
            (item for item in subtasks if item.get("subtask_id") == task.get("current_subtask_id")),
            None,
        )
        reason = task.get("reason") if isinstance(task.get("reason"), dict) else {}
        status = str(task["status"])
        result = None
        error = None
        if status == "succeeded":
            result = {"summary": str(reason.get("summary") or "Completed.")[:1500]}
        elif status in {"failed", "cancelled"}:
            error = {
                "code": str(reason.get("code") or "TASK_FAILED")[:128],
                "summary": str(reason.get("summary") or "")[:1500],
            }
        raw = self.runtime_store.get_task_for_owner(
            task_id, owner_principal_id=principal_id, controller_id=controller_id
        )
        origin = {
            key: raw.origin[key]
            for key in (
                "dsh_session_id", "created_execution_id", "created_turn_id",
                "created_tool_call_id", "root_call_id",
            )
            if key in raw.origin and isinstance(raw.origin[key], (str, int))
        }
        device = self.device_projection(principal_id, controller_id, task_id) if self.device_projection else self._task_device(
            principal_id, controller_id, subtasks
        )
        return {
            "schema_version": 2,
            "task_id": task_id,
            "origin": origin,
            "goal": {"summary": str(session.original_instruction)[:1500]},
            "current_revision": int(task["current_revision"]),
            "status": status,
            "reason": {
                "code": str(reason.get("code") or "TASK_STATUS_UNKNOWN")[:128],
                "summary": str(reason.get("summary") or "")[:1500],
                "recoverable": bool(reason.get("recoverable", False)),
            },
            "priority": int(task.get("priority", 50)),
            "current": {
                "stage": current_subtask.get("current_stage") if current_subtask else None,
                "action": None,
            },
            "next_wake_at": task.get("next_wake_at"),
            "event_cursor": int(session.event_cursor),
            "progress": {"kind": "unknown", "completed": None, "total": None},
            "pending_question": self._pending_question(task_id),
            "result": result,
            "error": error,
            "integrity": {
                "state": str(task.get("integrity_state") or "clear"),
                "reason_code": task.get("integrity_reason_code"),
            },
            "allowed_controls": [
                value for value in task.get("allowed_controls", [])
                if value in {"pause", "resume", "cancel", "takeover", "release_takeover"}
            ],
            "subtasks": subtasks,
            "device": device,
            "archived": task.get("archived_at") is not None,
            "timestamps": {
                "created_at": str(task["created_at"]),
                "updated_at": str(task["updated_at"]),
                "terminal_at": task.get("terminal_at"),
            },
        }

    def _pending_question(self, task_id: str) -> dict[str, Any] | None:
        if self.fact_questions is None:
            return None
        needs = self.fact_questions.facts.store.list_needs(
            session_id=task_id, status=NeedUserFactStatus.OPEN
        )
        if not needs:
            return None
        need = needs[0]
        return {
            "question_id": str(need.id),
            "question": str(need.question)[:1500],
            "why_needed": str(need.why_needed)[:1500],
        }

    def _task_device(
        self, principal_id: str, controller_id: str, subtasks: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if self.profiles is None:
            return None
        selected = next(
            (
                item for item in subtasks
                if item.get("kind") == "android_ui_agent"
                and isinstance(item.get("object_ref"), str)
            ),
            None,
        )
        if selected is None:
            return None
        try:
            profile = self.profiles.require_profile(
                principal_id=principal_id,
                controller_id=controller_id,
                profile_id=str(selected["object_ref"]),
            ).public_projection()
        except Exception:
            return None
        return {
            "profile_id": str(profile["profile_id"]),
            "display_name": str(profile["display_name"])[:80],
            "state": str(profile["state"])[:64],
        }

    def _canonical_owner(self, task_id: str) -> tuple[str, str] | None:
        alias = self.execution_store.v2_alias_for_task(task_id)
        if alias is None:
            return None
        try:
            task = self.runtime_store.get_task(task_id)
        except Exception:
            return None
        if not (
            _same(str(alias["principal_id"]), str(task.owner_principal_id))
            and _same(str(alias["controller_id"]), str(task.controller_id))
        ):
            raise ExecutionContractError("TASK_INTEGRITY_BLOCKED", "Task ownership is inconsistent.", 409)
        return str(task.owner_principal_id), str(task.controller_id)

    def _required_owner(self, task_id: str) -> tuple[str, str]:
        owner = self._canonical_owner(task_id)
        if owner is None:
            raise ExecutionContractError("TASK_NOT_FOUND", "The task was not found.", 404)
        return owner

    def _service_for_request(self, task_id: str, request: dict[str, Any]) -> CanonicalTaskService:
        owner = self._required_owner(task_id)
        requested = str(request["principal_id"]), str(request["controller_id"])
        if not (_same(owner[0], requested[0]) and _same(owner[1], requested[1])):
            raise ExecutionContractError("TASK_NOT_FOUND", "The task was not found.", 404)
        return CanonicalTaskService(
            self.runtime_store, principal_id=requested[0], controller_id=requested[1]
        )

    @staticmethod
    def _cursor(value: Any) -> int:
        if value is None:
            return 0
        if not isinstance(value, str) or not value.isdigit():
            raise ValueError("invalid page cursor")
        return int(value)


@dataclass(frozen=True, slots=True)
class _CandidateLease:
    principal_id: str
    controller_id: str
    identity_digest: str
    expires_at: float


class EmulatorProfilePortAdapter:
    """Owner-scoped profile port with short-lived, opaque discovery handles."""

    def __init__(
        self,
        *,
        profiles: Any,
        store: Any,
        discover: Callable[[], Any],
        clock: Callable[[], str],
        monotonic: Callable[[], float] = time.monotonic,
        candidate_ttl_seconds: float = 60.0,
    ) -> None:
        self.profiles = profiles
        self.store = store
        self.discover = discover
        self.clock = clock
        self.monotonic = monotonic
        self.candidate_ttl_seconds = candidate_ttl_seconds
        self._candidates: dict[str, _CandidateLease] = {}

    def discover_emulators(self, request: dict[str, Any]) -> dict[str, Any]:
        principal_id, controller_id = self._owner(request)
        result = self.discover()
        devices = self.profiles.discover_candidates(
            principal_id=principal_id,
            controller_id=controller_id,
            devices=tuple(_value(result, "devices", ())),
        )
        now = self.monotonic()
        self._candidates = {
            key: value for key, value in self._candidates.items() if value.expires_at > now
        }
        items = []
        for device in devices:
            candidate_id = f"candidate_{secrets.token_hex(16)}"
            self._candidates[candidate_id] = _CandidateLease(
                principal_id, controller_id, self._device_digest(device),
                now + self.candidate_ttl_seconds,
            )
            properties = _value(device, "properties", {}) or {}
            label = next((properties.get(key) for key in ("model", "product", "device") if properties.get(key)), "Running Android emulator")
            items.append({
                "candidate_id": candidate_id,
                "display_name": str(label)[:80],
                "kind": "android_emulator",
                "state": "ready",
            })
        return {"items": items, "expires_in_seconds": int(self.candidate_ttl_seconds)}

    def list(self, request: dict[str, Any]) -> dict[str, Any]:
        principal_id, controller_id = self._owner(request)
        items = [self._project(item) for item in self.profiles.projections(
            principal_id=principal_id, controller_id=controller_id
        )]
        return {"profiles": items[: int(request.get("limit", 100))], "next_cursor": None}

    def get(self, profile_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        principal_id, controller_id = self._owner(request)
        try:
            profile = self.profiles.require_profile(
                principal_id=principal_id, controller_id=controller_id, profile_id=profile_id
            )
        except Exception:
            return None
        return self._project(profile.public_projection())

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        principal_id, controller_id = self._owner(request)
        payload = request.get("profile") or {}
        candidate_id = str(payload.get("candidate_id") or "")
        lease = self._candidates.get(candidate_id)
        now = self.monotonic()
        if lease is None or lease.expires_at <= now or not (
            _same(lease.principal_id, principal_id) and _same(lease.controller_id, controller_id)
        ):
            raise ExecutionContractError("EMULATOR_CANDIDATE_EXPIRED", "The emulator candidate is unavailable.", 409)
        result = self.discover()
        devices = self.profiles.discover_candidates(
            principal_id=principal_id,
            controller_id=controller_id,
            devices=tuple(_value(result, "devices", ())),
        )
        matches = [item for item in devices if _same(self._device_digest(item), lease.identity_digest)]
        if len(matches) != 1:
            raise ExecutionContractError("EMULATOR_CANDIDATE_CHANGED", "The emulator candidate changed.", 409)
        profile = self.profiles.save_selected(
            principal_id=principal_id,
            controller_id=controller_id,
            candidate=matches[0],
            display_name=str(payload.get("display_name") or "Android emulator")[:80],
            is_default=bool(payload.get("is_default", False)),
        )
        self._candidates.pop(candidate_id, None)
        return self._project(profile.public_projection())

    def update(self, profile_id: str, request: dict[str, Any]) -> dict[str, Any]:
        principal_id, controller_id = self._owner(request)
        action = str((request.get("profile") or {}).get("action") or "")
        if action == "set_default":
            profile = self.profiles.set_default(
                principal_id=principal_id, controller_id=controller_id, profile_id=profile_id
            )
        elif action == "disable":
            profile = self.profiles.disable(
                principal_id=principal_id, controller_id=controller_id, profile_id=profile_id
            )
        elif action == "rename":
            payload = request.get("profile") or {}
            profile = self.profiles.require_profile(
                principal_id=principal_id, controller_id=controller_id, profile_id=profile_id
            )
            if int(payload.get("expected_revision", -1)) != profile.profile_generation:
                raise ExecutionContractError(
                    "DEVICE_PROFILE_REVISION_CONFLICT", "The profile revision is stale.", 409
                )
            display_name = str(payload.get("display_name") or "").strip()
            if not display_name or len(display_name) > 80:
                raise ExecutionContractError(
                    "DEVICE_PROFILE_UPDATE_INVALID", "The profile update is invalid.", 409
                )
            # D owns the row and owner check; H only adapts the already-shipped
            # renderer rename shape without inventing a profile store.
            profile = self.store.save_profile(
                replace(profile, display_name=display_name, updated_at=self.clock())
            )
        else:
            raise ExecutionContractError("DEVICE_PROFILE_UPDATE_UNSUPPORTED", "The profile update is unsupported.", 409)
        return self._project(profile.public_projection())

    def verify(self, profile_id: str, request: dict[str, Any]) -> dict[str, Any]:
        principal_id, controller_id = self._owner(request)
        profile = self.profiles.verify(
            principal_id=principal_id, controller_id=controller_id, profile_id=profile_id
        )
        return self._project(profile.public_projection())

    @staticmethod
    def _owner(request: dict[str, Any]) -> tuple[str, str]:
        return str(request["principal_id"]), str(request["controller_id"])

    @staticmethod
    def _device_digest(device: Any) -> str:
        payload = {
            "serial": str(_value(device, "serial", "")),
            "properties": dict(_value(device, "properties", {}) or {}),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _project(source: dict[str, Any]) -> dict[str, Any]:
        features = source.get("capabilities") if isinstance(source.get("capabilities"), list) else []
        return {
            "schema_version": 2,
            "device_profile_id": str(source["profile_id"]),
            "revision": int(source.get("profile_generation", 1)),
            "display_name": str(source.get("display_name") or "Android emulator")[:80],
            "kind": "android_emulator",
            "state": str(source.get("state") or "unavailable"),
            "is_default": bool(source.get("is_default", False)),
            "enabled": bool(source.get("enabled", True)),
            "capabilities": {"features": [str(item)[:64] for item in features[:32]]},
            "last_test": {
                "status": "verified" if source.get("last_verified_at") else "unknown",
                "checked_at": source.get("last_verified_at"),
                "reason_code": source.get("last_error_code"),
            },
            "timestamps": {
                "created_at": source.get("created_at"),
                "updated_at": source.get("updated_at"),
            },
        }


class ExperiencePortAdapter:
    """Expose Package E's safe projection only; hints never dispatch actions."""

    def __init__(self, experience: Any) -> None:
        self.experience = experience

    def list_for_task(self, task_id: str, *, cursor: str | None, limit: int) -> dict[str, Any]:
        try:
            episode = self.experience.store.episode_for_task(task_id)
        except KeyError:
            return {"items": [], "next_cursor": None}
        candidates: Iterable[Any] = self.experience.store.candidates(episode.scope)
        items = []
        for candidate in candidates:
            scope = _value(candidate, "scope")
            task_scope = str(_value(scope, "task_scope", "generic-task"))
            if task_scope not in {"generic-task", task_id}:
                continue
            projection = self.experience.project_experience(str(_value(candidate, "candidate_id")))
            items.append({key: _jsonable(value) for key, value in asdict(projection).items()})
            if len(items) >= limit:
                break
        return {"items": items, "next_cursor": None}


@dataclass(frozen=True, slots=True)
class VerifiedFrameBinding:
    task_id: str
    profile_id: str
    observation_id: str
    frame_reference: str


class VerifiedFramePort:
    """Validate an explicit Settings-run observation before exposing PNG bytes."""

    def __init__(
        self,
        *,
        resolve_binding: Callable[[str, str, str], VerifiedFrameBinding | None],
        load_observation: Callable[[str], Any],
        profiles: Any,
        artifacts: Any,
    ) -> None:
        self.resolve_binding = resolve_binding
        self.load_observation = load_observation
        self.profiles = profiles
        self.artifacts = artifacts

    def metadata(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        binding, observation = self._verified(task_id, request)
        screenshot = observation.screenshot
        artifact = screenshot.artifact
        return {
            "schema_version": 2,
            "frame_id": str(observation.id),
            "task_id": task_id,
            "device_profile_id": binding.profile_id,
            "content_type": "image/png",
            "size_bytes": int(artifact.size_bytes),
            "sha256": str(artifact.sha256),
            "width": int(screenshot.width),
            "height": int(screenshot.height),
            "captured_at": str(screenshot.captured_at),
        }

    def content(self, task_id: str, frame_id: str, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        metadata = self.metadata(task_id, request)
        if not _same(str(metadata["frame_id"]), frame_id):
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 404)
        binding, observation = self._verified(task_id, request)
        try:
            content = self.artifacts.read(observation.screenshot.artifact)
        except Exception:
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 404) from None
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 409)
        return metadata, content

    def _verified(self, task_id: str, request: dict[str, Any]) -> tuple[VerifiedFrameBinding, Any]:
        principal_id = str(request["principal_id"])
        controller_id = str(request["controller_id"])
        binding = self.resolve_binding(principal_id, controller_id, task_id)
        if binding is None or not _same(binding.task_id, task_id):
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 404)
        try:
            profile = self.profiles.require_profile(
                principal_id=principal_id,
                controller_id=controller_id,
                profile_id=binding.profile_id,
            )
            observation = self.load_observation(binding.observation_id)
        except Exception:
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 404) from None
        screenshot = observation.screenshot
        artifact = screenshot.artifact
        if (
            str(observation.id) != binding.observation_id
            or str(observation.device_id) != str(profile.canonical_device_id)
            or str(artifact.reference) != binding.frame_reference
            or str(screenshot.mime_type) != "image/png"
            or str(artifact.content_type) != "image/png"
        ):
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 409)
        try:
            content = self.artifacts.read(artifact)
        except Exception:
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 404) from None
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 409)
        return binding, observation


__all__ = [
    "CanonicalTaskPortAdapter", "EmulatorProfilePortAdapter", "ExperiencePortAdapter",
    "VerifiedFrameBinding", "VerifiedFramePort",
]
