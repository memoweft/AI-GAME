"""Thin projection from authoritative AgentSession/Kernel facts to execution v1."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict

from ..runtime_kernel.observation import ArtifactRef
from ..android_ui_runtime.sanitizer import sanitize_task_goal, sanitize_task_payload
from .store import SQLiteExecutionContractStore
from .v2_models import TASK_STATUSES


STATUSES = (
    "running", "waiting_event", "needs_user_input",
    "succeeded", "failed", "cancelled",
)

PHONE_EXECUTION_POLICY = {
    "execution_origin": "weftmate-harness-v1",
    "require_physical_android_action": True,
    "required_adapter": "android-companion-v1",
    "allowed_actions": ["OPEN_APP"],
    "action": {"type": "OPEN_APP", "package": "com.android.settings"},
    "expected_device": {"kind": "configured_canonical"},
    "expected_final_state": {"foreground_package": "com.android.settings"},
    "physical_execution_count": 1,
}

PHONE_CONFIRMATION_QUESTION = "是否继续打开系统设置？"
PHONE_CONFIRMATION_ANSWER = "继续"
DSH_AUTHORIZATION_MODES = frozenset({"full-access", "allowed-once"})


class ExecutionContractError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


class ExecutionContractService:
    """Own only external correlation; internal runtimes remain authoritative."""

    version = "1.0"

    def __init__(
        self,
        store: SQLiteExecutionContractStore,
        sessions: Any,
        fact_questions: Any,
        *,
        kernel: Any | None = None,
        artifact_store: Any | None = None,
        phone_runtime: Any | None = None,
        require_phone_policy: bool = False,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.fact_questions = fact_questions
        self.kernel = kernel
        self.artifact_store = artifact_store
        self.phone_runtime = phone_runtime
        self.require_phone_policy = require_phone_policy

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "version": self.version,
            "legacy_read_only": True,
            "capabilities": {
                "submit": False,
                "inspect": True,
                "events_replay": True,
                "cancel": False,
                "resume": False,
                "answer_pending_question": False,
                "evidence_read": self.kernel is not None and self.artifact_store is not None,
            },
        }

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        execution_mode = self._execution_mode(request)
        identity_key = self.identity_key(request["identity"])
        payload_json = _json(request)
        payload_hash = _sha(payload_json)
        execution_id = f"exec_{_sha(identity_key)[:32]}"
        now = _now()
        record, created = self.store.reserve({
            "execution_id": execution_id,
            "identity_key": identity_key,
            "payload_hash": payload_hash,
            "payload_json": payload_json,
            "dsh_session_id": request["identity"]["dsh_session_id"],
            "dsh_turn_id": str(request["identity"]["dsh_turn_id"]),
            "tool_call_id": request["identity"]["tool_call_id"],
            "root_call_id": request["identity"]["root_call_id"],
            "client_request_id": request["client_request_id"],
            "idempotency_key": request["idempotency_key"],
            "internal_client_request_id": f"weftmate-harness-{_sha(identity_key)[:48]}",
            "execution_mode": execution_mode,
            "created_at": now,
            "updated_at": now,
        })
        if not created and record["payload_hash"] != payload_hash:
            raise ExecutionContractError(
                "EXECUTION_IDEMPOTENCY_CONFLICT",
                "The same DSH tool identity was already used with a different request.",
                409,
            )
        if execution_mode == "physical_android_companion":
            if self.phone_runtime is None:
                raise ExecutionContractError(
                    "PHYSICAL_ANDROID_RUNTIME_UNAVAILABLE",
                    "The trusted physical Android execution runtime is unavailable.",
                    503,
                )
            task_id = record.get("kernel_task_id")
            if record["internal_session_id"] is None or task_id is None:
                try:
                    owner = self.phone_runtime.start(
                        execution_id=execution_id,
                        payload=json.loads(str(record["payload_json"])),
                        internal_client_request_id=str(
                            record["internal_client_request_id"]
                        ),
                    )
                except Exception as error:
                    raise self._runtime_error(
                        error, "EXECUTION_SUBMIT_UNAVAILABLE"
                    ) from None
                session_id = _required_string(
                    owner.get("session_id"), "internal session id"
                )
                task_id = _required_string(owner.get("task_id"), "Kernel task id")
                self.store.bind_internal(execution_id, session_id, _now())
                self.store.bind_kernel_task(execution_id, task_id, _now())
            payload = json.loads(str(record["payload_json"]))
            authorization_mode = self._dsh_authorization_mode(payload)
            if (
                authorization_mode is not None
                and record.get("phone_confirmed_at") is None
            ):
                authorized_at = _now()
                self.store.confirm_phone_execution(
                    execution_id,
                    self._dsh_authorization_operation_key(
                        execution_id, authorization_mode
                    ),
                    authorized_at,
                )
                self.store.append_event(
                    execution_id,
                    f"dsh-authorization:{authorization_mode}",
                    "execution.authorized",
                    {"authorization_mode": authorization_mode},
                    authorized_at,
                )
                # The durable marker is committed before dispatch.  A crash
                # after this point is resumed by recover_phone_executions(),
                # under the same execution/task identity.
                self.phone_runtime.kick(
                    execution_id=execution_id,
                    task_id=_required_string(task_id, "Kernel task id"),
                )
        elif record["internal_session_id"] is None:
            instruction = self._instruction(json.loads(str(record["payload_json"])))
            try:
                internal = self.sessions.create(
                    instruction, str(record["internal_client_request_id"])
                )
            except Exception as error:
                raise self._runtime_error(error, "EXECUTION_SUBMIT_UNAVAILABLE") from None
            session_id = _required_string(internal.get("id"), "internal session id")
            self.store.bind_internal(execution_id, session_id, _now())
        self.store.append_event(
            execution_id, "execution:accepted", "execution.accepted",
            {"execution_id": execution_id, "replayed": not created}, now,
        )
        if (
            execution_mode == "physical_android_companion"
            and self._record(execution_id).get("phone_confirmed_at") is None
        ):
            self.store.append_event(
                execution_id,
                "phone-confirmation-required",
                "execution.question_required",
                {"question_id": self._phone_question_id(execution_id)},
                now,
            )
        return self.inspect(execution_id)

    def lookup(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        record = self.store.by_identity(self.identity_key(identity))
        return None if record is None else self.inspect(str(record["execution_id"]))

    def recover_phone_executions(self) -> None:
        """Resume only non-terminal persisted phone tasks under their original ids."""
        if self.phone_runtime is None:
            return
        for record in self.store.records_for_mode("physical_android_companion"):
            if record.get("phone_confirmed_at") is None:
                continue
            session_id = record.get("internal_session_id")
            task_id = record.get("kernel_task_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            if not isinstance(task_id, str) or not task_id:
                continue
            projection = self.phone_runtime.inspect(
                execution_id=str(record["execution_id"]),
                session_id=session_id,
                task_id=task_id,
            )
            if str(projection["status"]) in {
                "succeeded", "failed", "cancelled", "needs_user_input"
            }:
                continue
            self.phone_runtime.kick(
                execution_id=str(record["execution_id"]), task_id=task_id
            )

    def inspect(self, execution_id: str) -> dict[str, Any]:
        record = self._record(execution_id)
        if record.get("execution_mode") == "physical_android_companion":
            return self._inspect_phone(record)
        session_id = record.get("internal_session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ExecutionContractError(
                "EXECUTION_RECOVERY_PENDING",
                "The execution identity is durable but its internal owner is still recovering.",
                503,
            )
        try:
            projection = self.sessions.inspect(session_id)
            self._sync_internal_events(record, projection)
            self._sync_evidence(record, projection)
        except ExecutionContractError:
            raise
        except Exception as error:
            raise self._runtime_error(error, "EXECUTION_INSPECT_UNAVAILABLE") from None
        payload = json.loads(str(record["payload_json"]))
        question = self._pending_question(session_id)
        status, goal_run = _status(projection, question)
        event_cursor = self._latest_cursor(execution_id)
        error = _safe_error(goal_run, projection)
        return {
            "execution_id": execution_id,
            "status": status,
            "goal_summary": payload["goal"]["summary"],
            "current_stage": _first_text(
                goal_run.get("active_stage") if goal_run else None,
                (projection.get("current_goal") or {}).get("status"),
            ),
            "progress": {
                "kind": "unknown",
                "completed": None,
                "total": None,
                "explanation": "The runtime has no authoritative numeric completion measure.",
            },
            "current_action": None,
            "pending_question": question,
            "result_summary": _first_text(
                goal_run.get("result_summary") if goal_run else None,
                projection.get("summary"),
            ),
            "evidence_refs": [self._public_evidence(item) for item in self.store.evidence_for(execution_id)],
            "event_cursor": event_cursor,
            "timestamps": {
                "created_at": str(record["created_at"]),
                "updated_at": str(projection.get("updated_at") or record["updated_at"]),
                "terminal_at": (
                    _first_text(goal_run.get("terminal_at") if goal_run else None, projection.get("stopped_at"))
                    if status in {"succeeded", "failed", "cancelled"} else None
                ),
            },
            "error": error,
            "internal_pointer": {
                "session_id": session_id,
                "goal_id": (projection.get("current_goal") or {}).get("id"),
                "task_id": goal_run.get("bound_task_id") if goal_run else None,
            },
        }

    def events(self, execution_id: str, after: int, limit: int) -> dict[str, Any]:
        self.inspect(execution_id)
        items = self.store.events(execution_id, after, limit)
        return {
            "items": items,
            "count": len(items),
            "next_cursor": items[-1]["cursor"] if items else after,
        }

    def cancel(self, execution_id: str, operation_key: str) -> dict[str, Any]:
        record = self._record(execution_id)
        if record.get("execution_mode") == "physical_android_companion":
            return self._phone_control(record, "cancel", operation_key)
        return self._control(execution_id, "stop", "cancel", operation_key)

    def resume(self, execution_id: str, operation_key: str) -> dict[str, Any]:
        record = self._record(execution_id)
        if record.get("execution_mode") == "physical_android_companion":
            return self._phone_control(record, "resume", operation_key)
        return self._control(execution_id, "resume", "resume", operation_key)

    def answer(
        self, execution_id: str, need_id: str, value: Any, operation_key: str
    ) -> dict[str, Any]:
        record = self._record(execution_id)
        if record.get("execution_mode") == "physical_android_companion":
            return self._answer_phone(record, need_id, value, operation_key)
        session_id = _required_string(record.get("internal_session_id"), "internal session id")
        digest = _sha(_json({"need_id": need_id, "value": value}))
        try:
            already_applied = self.store.record_operation(
                execution_id, operation_key, "answer", digest, _now()
            )
        except ValueError:
            raise ExecutionContractError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "The operation key was already used with different input.", 409,
            ) from None
        if already_applied:
            return self.inspect(execution_id)
        question = self._pending_question(session_id)
        if question is None or question["question_id"] != need_id:
            raise ExecutionContractError(
                "PENDING_QUESTION_NOT_FOUND",
                "The execution has no matching pending question.",
                409,
            )
        try:
            self.fact_questions.answer(
                need_id=need_id,
                value=value,
                idempotency_key=f"harness-{_sha(execution_id + ':' + operation_key)[:48]}",
                provenance={"source": "weftmate-harness-v1", "execution_id": execution_id},
            )
        except Exception as error:
            raise self._runtime_error(error, "QUESTION_ANSWER_REJECTED") from None
        self.store.mark_operation_applied(execution_id, operation_key, _now())
        self.store.append_event(
            execution_id, f"answer:{operation_key}", "execution.question_answered",
            {"question_id": need_id}, _now(),
        )
        return self.inspect(execution_id)

    def read_evidence(self, execution_id: str, evidence_id: str) -> tuple[bytes, str]:
        self._record(execution_id)
        record = self.store.evidence(execution_id, evidence_id)
        if record is None:
            raise ExecutionContractError("EVIDENCE_NOT_FOUND", "Evidence was not found.", 404)
        if self.artifact_store is None:
            raise ExecutionContractError("EVIDENCE_UNAVAILABLE", "Evidence storage is unavailable.", 503)
        artifact = ArtifactRef(
            reference=str(record["reference"]),
            content_type=str(record["content_type"]),
            size_bytes=int(record["size_bytes"]),
            sha256=str(record["sha256"]),
        )
        try:
            return self.artifact_store.read(artifact), artifact.content_type
        except Exception:
            raise ExecutionContractError(
                "EVIDENCE_UNAVAILABLE", "Evidence failed bounded integrity verification.", 503
            ) from None

    @staticmethod
    def identity_key(identity: dict[str, Any]) -> str:
        return "\x1f".join((
            str(identity["dsh_session_id"]), str(identity["dsh_turn_id"]),
            str(identity["tool_call_id"]),
        ))

    def _record(self, execution_id: str) -> dict[str, Any]:
        record = self.store.get(execution_id)
        if record is None:
            raise ExecutionContractError("EXECUTION_NOT_FOUND", "Execution was not found.", 404)
        return record

    def _execution_mode(self, request: dict[str, Any]) -> str:
        constraints = request["goal"].get("constraints") or {}
        if constraints == PHONE_EXECUTION_POLICY:
            return "physical_android_companion"
        if self.require_phone_policy:
            raise ExecutionContractError(
                "PHONE_EXECUTION_POLICY_REJECTED",
                "The production execution seam accepts only the trusted Settings-only physical Companion policy.",
                409,
            )
        return "agent_session_compat"

    @staticmethod
    def _dsh_authorization_mode(payload: dict[str, Any]) -> str | None:
        """Return only the two DSH-issued modes that may skip legacy gating."""
        mode = payload.get("authorization_mode")
        return mode if mode in DSH_AUTHORIZATION_MODES else None

    @staticmethod
    def _dsh_authorization_operation_key(execution_id: str, mode: str) -> str:
        return f"dsh-authorization:{mode}:{_sha(execution_id)[:32]}"

    def _inspect_phone(self, record: dict[str, Any]) -> dict[str, Any]:
        if self.phone_runtime is None:
            raise ExecutionContractError(
                "PHYSICAL_ANDROID_RUNTIME_UNAVAILABLE",
                "The trusted physical Android execution runtime is unavailable.",
                503,
            )
        session_id = _required_string(
            record.get("internal_session_id"), "internal session id"
        )
        task_id = _required_string(record.get("kernel_task_id"), "Kernel task id")
        try:
            projection = self.phone_runtime.inspect(
                execution_id=str(record["execution_id"]),
                session_id=session_id,
                task_id=task_id,
            )
            for event in projection.get("events", ()):
                self.store.append_event(
                    str(record["execution_id"]),
                    f"kernel:{event['sequence']}",
                    "runtime.event",
                    {
                        "source_cursor": int(event["sequence"]),
                        "type": str(event["type"]),
                    },
                    str(event["created_at"]),
                )
            self._sync_phone_evidence(record, projection)
        except ExecutionContractError:
            raise
        except Exception as error:
            raise self._runtime_error(
                error, "EXECUTION_INSPECT_UNAVAILABLE"
            ) from None
        status = str(projection["status"])
        waiting_for_confirmation = (
            record.get("phone_confirmed_at") is None
            and status not in {"succeeded", "failed", "cancelled"}
        )
        if waiting_for_confirmation:
            status = "needs_user_input"
        payload = json.loads(str(record["payload_json"]))
        return {
            "execution_id": str(record["execution_id"]),
            "status": status,
            "goal_summary": payload["goal"]["summary"],
            "current_stage": (
                "Awaiting pre-action confirmation before Android dispatch"
                if waiting_for_confirmation
                else projection.get("current_stage")
            ),
            "progress": {
                "kind": "unknown",
                "completed": None,
                "total": None,
                "explanation": "The finite RuntimeKernel path exposes durable stages, not a guessed percentage.",
            },
            "current_action": (
                None if waiting_for_confirmation else projection.get("current_action")
            ),
            "pending_question": (
                {
                    "question_id": self._phone_question_id(str(record["execution_id"])),
                    "question": PHONE_CONFIRMATION_QUESTION,
                    "why_needed": (
                        "The trusted Settings-only policy requires explicit approval "
                        "before any physical Android action is dispatched."
                    ),
                    "answer_schema": {
                        "type": "string", "enum": [PHONE_CONFIRMATION_ANSWER]
                    },
                }
                if waiting_for_confirmation
                else None
            ),
            "result_summary": (
                None if waiting_for_confirmation else projection.get("result_summary")
            ),
            "evidence_refs": [
                self._public_evidence(item)
                for item in self.store.evidence_for(str(record["execution_id"]))
            ],
            "event_cursor": self._latest_cursor(str(record["execution_id"])),
            "timestamps": {
                "created_at": str(record["created_at"]),
                "updated_at": str(projection.get("updated_at") or record["updated_at"]),
                "terminal_at": projection.get("terminal_at"),
            },
            "error": projection.get("error"),
            "internal_pointer": {
                "session_id": session_id,
                "goal_id": None,
                "task_id": task_id,
            },
        }

    def _answer_phone(
        self, record: dict[str, Any], need_id: str, value: Any, operation_key: str
    ) -> dict[str, Any]:
        execution_id = str(record["execution_id"])
        expected_question_id = self._phone_question_id(execution_id)
        digest = _sha(_json({"need_id": need_id, "value": value}))
        try:
            already_applied = self.store.record_operation(
                execution_id, operation_key, "answer", digest, _now()
            )
        except ValueError:
            raise ExecutionContractError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "The operation key was already used with different input.", 409,
            ) from None
        if already_applied:
            return self.inspect(execution_id)

        current = self._record(execution_id)
        prior_operation = current.get("phone_confirmation_operation_key")
        if current.get("phone_confirmed_at") is not None:
            if prior_operation != operation_key:
                raise ExecutionContractError(
                    "PENDING_QUESTION_NOT_FOUND",
                    "The execution has no matching pending question.", 409,
                )
            # Recover the narrow crash window after confirmation commit but
            # before the generic operation row was marked applied.
            self.store.mark_operation_applied(execution_id, operation_key, _now())
            self.phone_runtime.kick(
                execution_id=execution_id,
                task_id=_required_string(current.get("kernel_task_id"), "Kernel task id"),
            )
            return self.inspect(execution_id)

        projection = self.phone_runtime.inspect(
            execution_id=execution_id,
            session_id=_required_string(
                current.get("internal_session_id"), "internal session id"
            ),
            task_id=_required_string(current.get("kernel_task_id"), "Kernel task id"),
        )
        if str(projection["status"]) in {"succeeded", "failed", "cancelled"}:
            raise ExecutionContractError(
                "PENDING_QUESTION_NOT_FOUND",
                "The execution has no matching pending question.", 409,
            )
        if need_id != expected_question_id:
            raise ExecutionContractError(
                "PENDING_QUESTION_NOT_FOUND",
                "The execution has no matching pending question.", 409,
            )
        normalized_answer = (
            value.get("value")
            if isinstance(value, dict) and set(value) == {"value"}
            else value
        )
        if normalized_answer != PHONE_CONFIRMATION_ANSWER:
            raise ExecutionContractError(
                "QUESTION_ANSWER_REJECTED",
                "The Settings confirmation answer must carry only the exact string '继续'.",
                409,
            )
        confirmed_at = _now()
        self.store.confirm_phone_execution(execution_id, operation_key, confirmed_at)
        self.store.mark_operation_applied(execution_id, operation_key, confirmed_at)
        self.store.append_event(
            execution_id,
            f"answer:{operation_key}",
            "execution.question_answered",
            {"question_id": need_id},
            confirmed_at,
        )
        self.phone_runtime.kick(
            execution_id=execution_id,
            task_id=_required_string(current.get("kernel_task_id"), "Kernel task id"),
        )
        return self.inspect(execution_id)

    @staticmethod
    def _phone_question_id(execution_id: str) -> str:
        return f"phone-confirm:{_sha(execution_id)[:32]}"

    def _phone_control(
        self, record: dict[str, Any], kind: str, operation_key: str
    ) -> dict[str, Any]:
        execution_id = str(record["execution_id"])
        digest = _sha(_json({"action": kind}))
        try:
            already_applied = self.store.record_operation(
                execution_id, operation_key, kind, digest, _now()
            )
        except ValueError:
            raise ExecutionContractError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "The operation key was already used with a different operation.",
                409,
            ) from None
        if not already_applied:
            try:
                getattr(self.phone_runtime, kind)(
                    execution_id=execution_id,
                    task_id=_required_string(record.get("kernel_task_id"), "Kernel task id"),
                )
            except Exception as error:
                raise self._runtime_error(
                    error, f"EXECUTION_{kind.upper()}_REJECTED"
                ) from None
            self.store.mark_operation_applied(execution_id, operation_key, _now())
            self.store.append_event(
                execution_id,
                f"{kind}:{operation_key}",
                f"execution.{kind}_requested",
                {"execution_id": execution_id},
                _now(),
            )
        return self.inspect(execution_id)

    def _sync_phone_evidence(
        self, record: dict[str, Any], projection: dict[str, Any]
    ) -> None:
        for evidence in projection.get("evidence", ()):
            self.store.put_evidence({
                "execution_id": str(record["execution_id"]),
                **evidence,
            })

    def _control(
        self, execution_id: str, action: str, kind: str, operation_key: str
    ) -> dict[str, Any]:
        record = self._record(execution_id)
        session_id = _required_string(record.get("internal_session_id"), "internal session id")
        digest = _sha(_json({"action": action}))
        try:
            already_applied = self.store.record_operation(
                execution_id, operation_key, kind, digest, _now()
            )
        except ValueError:
            raise ExecutionContractError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "The operation key was already used with a different operation.", 409,
            ) from None
        if not already_applied:
            try:
                self.sessions.control(
                    session_id, action,
                    f"harness-{kind}-{_sha(execution_id + ':' + operation_key)[:44]}",
                )
            except Exception as error:
                raise self._runtime_error(error, f"EXECUTION_{kind.upper()}_REJECTED") from None
            self.store.mark_operation_applied(execution_id, operation_key, _now())
            self.store.append_event(
                execution_id, f"{kind}:{operation_key}", f"execution.{kind}_requested",
                {"execution_id": execution_id}, _now(),
            )
        return self.inspect(execution_id)

    def _pending_question(self, session_id: str) -> dict[str, Any] | None:
        store = self.fact_questions.facts.store
        needs = store.list_needs(session_id=session_id)
        for need in reversed(needs):
            status = getattr(need.status, "value", need.status)
            if status != "OPEN":
                continue
            return {
                "question_id": need.id,
                "question": need.question,
                "why_needed": need.why_needed,
                "answer_schema": need.answer_schema,
            }
        return None

    def _sync_internal_events(self, record: dict[str, Any], projection: dict[str, Any]) -> None:
        session_id = str(record["internal_session_id"])
        after = 0
        while True:
            page = self.sessions.events(session_id, after=after, limit=500)
            for event in page:
                cursor = int(getattr(event, "cursor", 0))
                event_type = str(getattr(getattr(event, "event_type", None), "value", getattr(event, "event_type", "runtime")))
                self.store.append_event(
                    str(record["execution_id"]), f"agent:{cursor}", "runtime.event",
                    {"source_cursor": cursor, "type": event_type},
                    str(getattr(event, "occurred_at", None) or getattr(event, "created_at", None) or projection.get("updated_at") or _now()),
                )
            if len(page) < 500:
                break
            after = int(getattr(page[-1], "cursor", after))

    def _sync_evidence(self, record: dict[str, Any], projection: dict[str, Any]) -> None:
        if self.kernel is None or self.artifact_store is None:
            return
        for goal_run in (projection.get("goal_runs") or {}).values():
            task_id = goal_run.get("bound_task_id") if isinstance(goal_run, dict) else None
            if not task_id:
                continue
            try:
                observations = self.kernel.observations(task_id)
            except Exception:
                continue
            for observation in observations:
                artifacts = [observation.screenshot.artifact]
                if observation.ui_tree.artifact is not None:
                    artifacts.append(observation.ui_tree.artifact)
                for artifact in artifacts:
                    evidence_id = f"ev_{_sha(str(record['execution_id']) + ':' + artifact.reference)[:32]}"
                    self.store.put_evidence({
                        "evidence_id": evidence_id,
                        "execution_id": str(record["execution_id"]),
                        "reference": artifact.reference,
                        "content_type": artifact.content_type,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "created_at": observation.captured_at,
                    })

    def _latest_cursor(self, execution_id: str) -> int:
        return self.store.latest_cursor(execution_id)

    @staticmethod
    def _public_evidence(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": record["evidence_id"],
            "content_type": record["content_type"],
            "size_bytes": record["size_bytes"],
            "sha256": record["sha256"],
            "provenance": {
                "canonical_device_id": record.get("canonical_device_id"),
                "companion_install_id": record.get("companion_install_id"),
                "boot_id": record.get("boot_id"),
                "connection_epoch": record.get("connection_epoch"),
                "kernel_action_id": record.get("kernel_action_id"),
                "command_id": record.get("command_id"),
                "caused_by_command_id": record.get("caused_by_command_id"),
                "adapter_id": record.get("adapter_id"),
                "physical_execution_count": record.get("physical_execution_count"),
            },
            "read_path": (
                f"/api/execution/v1/executions/{record['execution_id']}"
                f"/evidence/{record['evidence_id']}"
            ),
        }

    @staticmethod
    def _instruction(payload: dict[str, Any]) -> str:
        constraints = payload["goal"].get("constraints") or {}
        if not constraints:
            return str(payload["goal"]["summary"])
        return f"{payload['goal']['summary']}\nStructured constraints: {_json(constraints)}"

    @staticmethod
    def _runtime_error(error: Exception, fallback: str) -> ExecutionContractError:
        raw = str(getattr(error, "code", "") or "").upper()
        allowed = {
            "DEVICE_NOT_AVAILABLE", "CAPABILITY_UNAVAILABLE", "TARGET_BUSY",
            "QUEUE_FULL", "SESSION_STATE_CONFLICT", "USER_FACT_ANSWER_INVALID",
        }
        code = raw if raw in allowed else fallback
        status = 409 if code in {"TARGET_BUSY", "SESSION_STATE_CONFLICT", "USER_FACT_ANSWER_INVALID"} else 503
        return ExecutionContractError(code, _public_message(code), status)


def _status(projection: dict[str, Any], question: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    if question is not None:
        return "needs_user_input", _goal_run(projection)
    goal = _goal_run(projection)
    execution = str((goal or {}).get("execution_status") or "").upper()
    session = str(projection.get("status") or "").upper()
    if execution == "COMPLETED" or session == "COMPLETED":
        return "succeeded", goal
    if execution == "CANCELLED" or session == "STOPPED":
        return "cancelled", goal
    if execution in {"FAILED", "UNCERTAIN"} or session in {"FAILED", "PARTIAL"}:
        return "failed", goal
    current = str((projection.get("current_goal") or {}).get("status") or "").upper()
    if execution.startswith("WAITING_") or current.startswith("WAITING_") or session == "WAITING_ALL":
        return "waiting_event", goal
    return "running", goal


def _goal_run(projection: dict[str, Any]) -> dict[str, Any] | None:
    current = projection.get("current_goal") or {}
    current_id = current.get("id")
    runs = projection.get("goal_runs") or {}
    value = runs.get(current_id) if current_id else projection.get("goal_run")
    return value if isinstance(value, dict) else None


def _safe_error(goal: dict[str, Any] | None, projection: dict[str, Any]) -> dict[str, Any] | None:
    raw = (goal or {}).get("error")
    if isinstance(raw, dict):
        code = str(raw.get("code") or "EXECUTION_FAILED").upper()
        return {"code": code[:128]}
    if str(projection.get("status") or "").upper() in {"FAILED", "PARTIAL"}:
        return {"code": "EXECUTION_FAILED"}
    return None


def _public_message(code: str) -> str:
    return {
        "DEVICE_NOT_AVAILABLE": "No authorized execution device is currently available.",
        "CAPABILITY_UNAVAILABLE": "The requested device capability is unavailable.",
        "TARGET_BUSY": "The selected device is busy.",
        "QUEUE_FULL": "The execution queue is full.",
        "SESSION_STATE_CONFLICT": "The execution does not accept this operation in its current state.",
        "USER_FACT_ANSWER_INVALID": "The pending question answer did not match its schema.",
    }.get(code, "The AI-Game execution runtime is unavailable.")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutionContractError("EXECUTION_RECOVERY_PENDING", f"{label} is unavailable.", 503)
    return value


def _first_text(*values: Any) -> str | None:
    return next((str(value) for value in values if isinstance(value, str) and value.strip()), None)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class V2TaskCreateRequest(TypedDict):
    """Internal-only creation input for the canonical AgentRuntime Task port."""

    task_id: None
    origin: dict[str, Any]
    goal: dict[str, Any]
    priority: int
    device_profile_id: str | None
    runner_kind: str | None
    runner_version: str | None
    client_request_id: str
    idempotency_key: str
    operation_id: str
    authorization_mode: str | None
    principal_id: str
    controller_id: str


class V2TaskPort(Protocol):
    """AgentRuntime's narrow canonical Task boundary used by the v2 adapter."""

    def create(self, request: V2TaskCreateRequest) -> dict[str, Any]: ...
    def get(self, task_id: str) -> dict[str, Any] | None: ...
    def list(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def events(self, task_id: str, *, after: int, limit: int) -> dict[str, Any]: ...
    def revise(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]: ...
    def control(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]: ...
    def answer(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]: ...
    def archive(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]: ...


class RunnerAdmissionPort(Protocol):
    """Owner-scoped immutable admission lookup consumed by a future scheduler."""

    def runner_admission(
        self, task_id: str, *, principal_id: str, controller_id: str,
    ) -> dict[str, Any] | None: ...


class V2DeviceProfilePort(Protocol):
    """Safe DeviceProfile contract; callers never receive raw emulator transport."""

    def list(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, profile_id: str, request: dict[str, Any]) -> dict[str, Any] | None: ...
    def create(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def update(self, profile_id: str, request: dict[str, Any]) -> dict[str, Any]: ...
    def verify(self, profile_id: str, request: dict[str, Any]) -> dict[str, Any]: ...
    def discover_emulators(self, request: dict[str, Any]) -> dict[str, Any]: ...


class V2ExperiencePort(Protocol):
    def list_for_task(self, task_id: str, *, cursor: str | None, limit: int) -> dict[str, Any]: ...


class V2FramePort(Protocol):
    """Explicit verified-frame seam; implementations must fail closed."""

    def metadata(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]: ...
    def content(
        self, task_id: str, frame_id: str, request: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]: ...


class V2ExecutionContractService:
    """Authenticated v2 adapter over canonical ports, never a Task state store.

    The store persists only the DSH execution alias, a capability principal/controller binding,
    mutation idempotency results, and an event-projection checkpoint.  Every
    task/revision/status/event decision is delegated to the injected
    AgentRuntime port.
    """

    version = "2.0"
    max_page_size = 200
    max_event_payload_bytes = 16_384

    def __init__(
        self,
        store: SQLiteExecutionContractStore,
        tasks: V2TaskPort,
        *,
        device_profiles: V2DeviceProfilePort | None = None,
        experiences: V2ExperiencePort | None = None,
        frames: V2FramePort | None = None,
        runner_ready: bool = True,
        runner_setup_reasons: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.tasks = tasks
        self.device_profiles = device_profiles
        self.experiences = experiences
        self.frames = frames
        self.runner_ready = runner_ready
        self.runner_setup_reasons = tuple(runner_setup_reasons)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.runner_ready else "needs_setup",
            "version": self.version,
            "capabilities": {
                "tasks": True, "revisions": True, "controls": True,
                "answers": True, "archive": True, "device_profiles": self.device_profiles is not None,
                "experience": self.experiences is not None,
                "emulator_discovery": self.device_profiles is not None,
                "verified_frames": self.frames is not None,
                "android_ui_agent": {
                    "state": "ready" if self.runner_ready else "needs_setup",
                    "available": self.runner_ready,
                    "version": "1",
                },
            },
            "setup_reasons": list(self.runner_setup_reasons),
        }

    def create_task(self, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        if not self.runner_ready:
            raise ExecutionContractError(
                "CAPABILITY_UNAVAILABLE",
                "The requested phone capability is unavailable.",
                409,
            )
        self._validate_runner_request(request)
        origin = request["origin"]
        identity_key = ExecutionContractService.identity_key(origin)
        payload_hash = _sha(_json(request))
        execution_id = f"exec_{_sha(identity_key)[:32]}"
        principal_id, controller_id = self._owner(auth_context)
        existing = self.store.v2_alias_by_identity(identity_key)
        if existing is not None:
            if not (
                hmac.compare_digest(str(existing["principal_id"]), principal_id)
                and hmac.compare_digest(str(existing["controller_id"]), controller_id)
            ):
                # An execution alias is provenance, not a cross-principal
                # lookup capability.  Do not reveal that it already exists.
                raise ExecutionContractError("TASK_NOT_FOUND", "The task was not found.", 404)
            if str(existing["payload_hash"]) != payload_hash:
                raise ExecutionContractError(
                    "EXECUTION_IDEMPOTENCY_CONFLICT",
                    "The same DSH tool identity was already used with a different request.", 409,
                )
            return self._task_response(str(existing["task_id"]), execution_id, replayed=True)

        submission_key = str(request["idempotency_key"])
        now = _now()
        principal_id, controller_id = self._owner(auth_context)
        envelope = self._canonical_create_envelope(
            request=request,
            principal_id=principal_id,
            controller_id=controller_id,
            execution_id=execution_id,
        )
        envelope_hash = _sha(_json(envelope))
        submission, submission_created = self.store.reserve_v2_submission({
            "submission_key": submission_key,
            "request_hash": self._submission_request_hash(request),
            "principal_id": principal_id,
            "controller_id": controller_id,
            "dsh_session_id": str(origin["dsh_session_id"]),
            "dsh_turn_id": str(origin["dsh_turn_id"]),
            "canonical_envelope_json": _json(envelope),
            "envelope_hash": envelope_hash,
            "runner_kind": envelope["runner_kind"],
            "runner_version": envelope["runner_version"],
            "device_profile_id": envelope["device_profile_id"],
            "authorization_mode": envelope["authorization_mode"],
            "canonical_client_request_id": envelope["client_request_id"],
            "canonical_idempotency_key": envelope["idempotency_key"],
            "canonical_origin_json": _json(envelope["origin"]),
            "canonical_origin_hash": _sha(_json(envelope["origin"])),
            "created_at": now,
            "updated_at": now,
        })
        self._validate_v2_submission(
            submission,
            request=request,
            principal_id=principal_id,
            controller_id=controller_id,
        )
        if not submission_created:
            task_id = submission.get("task_id")
            recovered_projection: dict[str, Any] | None = None
            if not isinstance(task_id, str) or not task_id:
                recovered = self._validated_submission_envelope(
                    submission, principal_id=principal_id, controller_id=controller_id,
                )
                recovered_projection = self._call_port(self.tasks.create, recovered)
                task_id = self._required_task_id(recovered_projection)
                self.store.bind_v2_submission(
                    principal_id=principal_id, controller_id=controller_id,
                    submission_key=submission_key, task_id=task_id, updated_at=_now(),
                )
            attempt, attempt_created = self.store.reserve_v2_alias(
                self._v2_alias_record(
                    execution_id=execution_id,
                    identity_key=identity_key,
                    payload_hash=payload_hash,
                    task_id=task_id,
                    submission_key=submission_key,
                    origin=origin,
                    principal_id=principal_id,
                    controller_id=controller_id,
                    envelope_hash=str(submission["envelope_hash"]),
                    runner_kind=str(submission["runner_kind"]),
                    runner_version=str(submission["runner_version"]),
                    now=now,
                )
            )
            if not attempt_created and (
                str(attempt["payload_hash"]) != payload_hash
                or str(attempt["task_id"]) != task_id
            ):
                raise ExecutionContractError(
                    "EXECUTION_IDEMPOTENCY_CONFLICT",
                    "The same DSH tool identity has conflicting task ownership.",
                    409,
                )
            return self._task_response(
                task_id, execution_id, replayed=True, projection=recovered_projection,
            )

        created = self._call_port(self.tasks.create, envelope)
        task_id = self._required_task_id(created)
        self.store.bind_v2_submission(
            principal_id=principal_id,
            controller_id=controller_id,
            submission_key=submission_key,
            task_id=task_id,
            updated_at=_now(),
        )
        record, was_created = self.store.reserve_v2_alias(
            self._v2_alias_record(
                execution_id=execution_id,
                identity_key=identity_key,
                payload_hash=payload_hash,
                task_id=task_id,
                submission_key=submission_key,
                origin=origin,
                principal_id=principal_id,
                controller_id=controller_id,
                envelope_hash=envelope_hash,
                runner_kind=str(envelope["runner_kind"]),
                runner_version=str(envelope["runner_version"]),
                now=now,
            )
        )
        if not was_created:
            if str(record["payload_hash"]) != payload_hash or str(record["task_id"]) != task_id:
                raise ExecutionContractError(
                    "EXECUTION_IDEMPOTENCY_CONFLICT",
                    "The same DSH tool identity has conflicting task ownership.", 409,
                )
        return self._task_response(task_id, execution_id, replayed=not was_created, projection=created)

    @staticmethod
    def _submission_request_hash(request: dict[str, Any]) -> str:
        # Hash the same normalized, secret-free goal representation that is
        # frozen in the canonical envelope.  This lets later admission prove
        # the request and envelope facts agree without retaining raw input.
        goal = _safe_create_goal(request.get("goal"))
        # The model may paraphrase the display summary when it retries the same
        # user turn.  All effect-bearing fields remain part of the fence hash.
        stable_goal = {key: value for key, value in goal.items() if key != "summary"}
        return _sha(_json({
            "client_request_id": request.get("client_request_id"),
            "idempotency_key": request.get("idempotency_key"),
            "goal": stable_goal,
            "priority": request.get("priority", 50),
            "device_profile_id": request.get("device_profile_id"),
            "runner_kind": request.get("runner_kind"),
            "runner_version": "1" if request.get("runner_kind") is not None else None,
            "authorization_mode": request.get("authorization_mode"),
        }))

    @staticmethod
    def _validate_runner_request(request: dict[str, Any]) -> None:
        runner_kind = request.get("runner_kind")
        if "runner_version" in request or runner_kind not in {None, "android_ui_agent"}:
            raise ExecutionContractError(
                "CAPABILITY_UNAVAILABLE",
                "The requested phone capability is unavailable.",
                409,
            )
        if runner_kind == "android_ui_agent" and not request.get("device_profile_id"):
            raise ExecutionContractError(
                "DEVICE_PROFILE_REQUIRED",
                "The Android emulator runner requires a saved device profile.",
                409,
            )

    @staticmethod
    def _canonical_create_envelope(
        *, request: dict[str, Any], principal_id: str, controller_id: str, execution_id: str,
    ) -> dict[str, Any]:
        """Freeze the first canonical create input before the cross-DB saga.

        The envelope is deliberately bounded and strips credential-shaped
        material before it becomes durable retry state.  A retry may have new
        tool provenance or paraphrased text, but it must replay this exact
        canonical request.
        """
        runner_kind = request.get("runner_kind")
        return {
            "task_id": None,
            "origin": V2ExecutionContractService._origin(request["origin"], execution_id),
            "goal": _safe_create_goal(request.get("goal")),
            "priority": int(request.get("priority", 50)),
            "device_profile_id": request.get("device_profile_id"),
            "runner_kind": runner_kind,
            "runner_version": "1" if runner_kind is not None else None,
            "client_request_id": str(request["client_request_id"]),
            "idempotency_key": str(request["idempotency_key"]),
            "operation_id": f"create:{execution_id}",
            "authorization_mode": request.get("authorization_mode"),
            "principal_id": principal_id,
            "controller_id": controller_id,
        }

    @staticmethod
    def _submission_envelope(submission: dict[str, Any]) -> dict[str, Any]:
        raw = submission.get("canonical_envelope_json")
        try:
            envelope = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            envelope = None
        if not isinstance(envelope, dict) or _sha(_json(envelope)) != submission.get("envelope_hash"):
            raise ExecutionContractError(
                "TASK_RECOVERY_PENDING",
                "The stable phone task submission cannot be safely recovered.", 503,
            )
        return envelope

    def _validated_submission_envelope(
        self, submission: dict[str, Any], *, principal_id: str, controller_id: str,
    ) -> dict[str, Any]:
        """Validate a persisted NULL-reservation before any canonical retry.

        This closes the cross-DB recovery gap: a damaged or partially migrated
        reservation remains dormant rather than being turned into a new Task.
        The checks intentionally occur before `tasks.create`, bind, or alias.
        """
        envelope = self._submission_envelope(submission)
        expected_keys = {
            "task_id", "origin", "goal", "priority", "device_profile_id", "runner_kind",
            "runner_version", "client_request_id", "idempotency_key", "operation_id",
            "authorization_mode", "principal_id", "controller_id",
        }
        if set(envelope) != expected_keys or envelope.get("task_id") is not None:
            return self._unsafe_envelope()
        origin = envelope.get("origin")
        goal = envelope.get("goal")
        if not isinstance(origin, dict) or not isinstance(goal, dict):
            return self._unsafe_envelope()
        expected_origin_keys = {
            "dsh_session_id", "created_execution_id", "created_turn_id",
            "created_tool_call_id", "root_call_id",
        }
        if set(origin) != expected_origin_keys or any(
            not self._bounded_identifier(origin.get(key), 256) for key in expected_origin_keys
        ):
            return self._unsafe_envelope()
        try:
            stored_origin = json.loads(str(submission.get("canonical_origin_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._unsafe_envelope()
        if not isinstance(stored_origin, dict) or not (
            _sha(_json(stored_origin)) == submission.get("canonical_origin_hash")
            and stored_origin == origin
            and str(origin["dsh_session_id"]) == str(submission.get("dsh_session_id") or "")
            and str(origin["created_turn_id"]) == str(submission.get("dsh_turn_id") or "")
        ):
            return self._unsafe_envelope()
        if not (
            envelope.get("principal_id") == principal_id == submission.get("principal_id")
            and envelope.get("controller_id") == controller_id == submission.get("controller_id")
            and envelope.get("device_profile_id") == submission.get("device_profile_id")
            and envelope.get("runner_kind") == submission.get("runner_kind")
            and envelope.get("runner_version") == submission.get("runner_version")
            and envelope.get("authorization_mode") == submission.get("authorization_mode")
            and envelope.get("client_request_id") == submission.get("canonical_client_request_id")
            and envelope.get("idempotency_key") == submission.get("canonical_idempotency_key")
        ):
            return self._unsafe_envelope()
        runner_kind = envelope.get("runner_kind")
        if runner_kind not in {None, "android_ui_agent"} or (
            runner_kind is None and envelope.get("runner_version") is not None
        ) or (runner_kind is not None and envelope.get("runner_version") != "1"):
            return self._unsafe_envelope()
        if runner_kind is not None and not self._bounded_identifier(envelope.get("device_profile_id"), 256):
            return self._unsafe_envelope()
        if envelope.get("authorization_mode") not in DSH_AUTHORIZATION_MODES:
            return self._unsafe_envelope()
        if type(envelope.get("priority")) is not int or not 0 <= envelope["priority"] <= 100:
            return self._unsafe_envelope()
        if not (
            self._bounded_identifier(envelope.get("client_request_id"), 128)
            and self._bounded_identifier(envelope.get("idempotency_key"), 128)
            and envelope.get("operation_id") == f"create:{origin['created_execution_id']}"
        ):
            return self._unsafe_envelope()
        expected_goal_keys = {"summary", "task_kind", "stop_condition", "schedule"}
        if (
            set(goal) != expected_goal_keys or not isinstance(goal.get("summary"), str)
            or not goal["summary"] or goal.get("task_kind") not in {"bounded", "open_ended"}
            or sanitize_task_goal(goal["task_kind"], maximum=32) != goal["task_kind"]
        ):
            return self._unsafe_envelope()
        if (
            len(goal["summary"]) > 1500
            or sanitize_task_goal(goal["summary"], maximum=1500) != goal["summary"]
            or not isinstance(goal.get("stop_condition"), dict)
            or (goal.get("schedule") is not None and not isinstance(goal.get("schedule"), dict))
            or sanitize_task_payload(goal.get("stop_condition"), maximum_string=1500, maximum_items=30) != goal.get("stop_condition")
            or sanitize_task_payload(goal.get("schedule"), maximum_string=1500, maximum_items=30) != goal.get("schedule")
        ):
            return self._unsafe_envelope()
        expected_hash = self._submission_hash_from_envelope(envelope)
        if (
            _sha(_json(envelope)) != submission.get("envelope_hash")
            or expected_hash != submission.get("request_hash")
        ):
            return self._unsafe_envelope()
        return envelope

    @staticmethod
    def _unsafe_envelope() -> None:
        raise ExecutionContractError(
            "TASK_RECOVERY_PENDING",
            "The stable phone task submission cannot be safely recovered.", 503,
        )

    @staticmethod
    def _bounded_identifier(value: Any, maximum: int) -> bool:
        return isinstance(value, str) and 0 < len(value) <= maximum and all(
            char.isascii() and (char.isalnum() or char in "._:-") for char in value
        )

    @staticmethod
    def _submission_hash_from_envelope(envelope: dict[str, Any]) -> str:
        goal = envelope.get("goal") if isinstance(envelope.get("goal"), dict) else {}
        stable_goal = {key: value for key, value in goal.items() if key != "summary"}
        return _sha(_json({
            "client_request_id": envelope.get("client_request_id"),
            "idempotency_key": envelope.get("idempotency_key"),
            "goal": stable_goal,
            "priority": envelope.get("priority", 50),
            "device_profile_id": envelope.get("device_profile_id"),
            "runner_kind": envelope.get("runner_kind"),
            "runner_version": envelope.get("runner_version"),
            "authorization_mode": envelope.get("authorization_mode"),
        }))

    def runner_admission(
        self, task_id: str, *, principal_id: str, controller_id: str,
    ) -> dict[str, Any] | None:
        """Expose no-side-effect admission facts for the resident scheduler."""
        if not self.runner_ready:
            return None
        fact = self.store.v2_runner_admission(
            task_id=task_id, principal_id=principal_id, controller_id=controller_id,
        )
        if fact is None:
            return None
        # Keep envelope validation inside the adapter rather than asking a
        # scheduler to reconstruct secret-sensitive create input.
        submission = self.store.v2_submission(
            principal_id, controller_id, str(fact["submission_key"]),
        )
        if submission is None:
            return None
        try:
            self._validated_submission_envelope(
                submission, principal_id=principal_id, controller_id=controller_id,
            )
        except ExecutionContractError:
            return None
        return fact

    def _validate_v2_submission(
        self,
        submission: dict[str, Any],
        *,
        request: dict[str, Any],
        principal_id: str,
        controller_id: str,
    ) -> None:
        origin = request["origin"]
        expected = (
            principal_id,
            controller_id,
            str(origin["dsh_session_id"]),
            str(origin["dsh_turn_id"]),
            self._submission_request_hash(request),
        )
        actual = tuple(str(submission.get(key) or "") for key in (
            "principal_id", "controller_id", "dsh_session_id", "dsh_turn_id", "request_hash",
        ))
        if not all(hmac.compare_digest(left, right) for left, right in zip(expected, actual)):
            raise ExecutionContractError(
                "EXECUTION_IDEMPOTENCY_CONFLICT",
                "The stable phone task submission conflicts with an existing request.",
                409,
            )

    @staticmethod
    def _v2_alias_record(
        *,
        execution_id: str,
        identity_key: str,
        payload_hash: str,
        task_id: str,
        submission_key: str,
        origin: dict[str, Any],
        principal_id: str,
        controller_id: str,
        envelope_hash: str,
        runner_kind: str,
        runner_version: str,
        now: str,
    ) -> dict[str, Any]:
        return {
            "execution_id": execution_id,
            "identity_key": identity_key,
            "payload_hash": payload_hash,
            "task_id": task_id,
            "submission_key": submission_key,
            "principal_id": principal_id,
            "controller_id": controller_id,
            "dsh_session_id": str(origin["dsh_session_id"]),
            "dsh_turn_id": str(origin["dsh_turn_id"]),
            "tool_call_id": str(origin["tool_call_id"]),
            "root_call_id": str(origin["root_call_id"]),
            "envelope_hash": envelope_hash,
            "runner_kind": runner_kind,
            "runner_version": runner_version,
            "created_at": now,
            "updated_at": now,
        }

    def list_tasks(
        self, origin: dict[str, Any] | None, *, auth_context: dict[str, Any], status: str | None, cursor: str | None, limit: int,
    ) -> dict[str, Any]:
        self._validated_status(status)
        principal_id, controller_id = self._owner(auth_context)
        allowed = self.store.v2_tasks_for_owner(principal_id, controller_id)
        page = self._call_port(self.tasks.list, {
            "task_ids": allowed, "status": status, "cursor": cursor, "limit": self._limit(limit),
        })
        items = page.get("items", []) if isinstance(page, dict) else []
        if not isinstance(items, list):
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned an invalid list.", 503)
        return {
            "items": [self._safe_task(item) for item in items if self._task_is_allowed(item, allowed)],
            "next_cursor": self._safe_cursor(page.get("next_cursor")),
        }

    def get_task(self, task_id: str, origin: dict[str, Any] | None, *, auth_context: dict[str, Any]) -> dict[str, Any]:
        alias = self._owned_alias(task_id, auth_context)
        return self._task_response(task_id, str(alias["execution_id"]))

    def events(
        self, task_id: str, origin: dict[str, Any] | None, *, auth_context: dict[str, Any], after: int, limit: int,
    ) -> dict[str, Any]:
        self._owned_alias(task_id, auth_context)
        if after < 0:
            raise ExecutionContractError("EVENT_CURSOR_INVALID", "The event cursor is invalid.", 409)
        page = self._call_port(self.tasks.events, task_id, after=after, limit=self._limit(limit))
        items = page.get("items", []) if isinstance(page, dict) else []
        if not isinstance(items, list):
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned invalid events.", 503)
        safe_items = [self._safe_event(item) for item in items]
        next_cursor = page.get("next_cursor", after) if isinstance(page, dict) else after
        if not isinstance(next_cursor, int) or next_cursor < after:
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned an invalid cursor.", 503)
        self.store.checkpoint_v2_projection(task_id, next_cursor, _now())
        return {"items": safe_items, "count": len(safe_items), "next_cursor": next_cursor}

    def revise(self, task_id: str, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        return self._mutation(task_id, request, auth_context, "revision", "revision_id", self.tasks.revise)

    def control(self, task_id: str, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        return self._mutation(task_id, request, auth_context, "control", "control_id", self.tasks.control)

    def answer(self, task_id: str, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        return self._mutation(task_id, request, auth_context, "answer", "question_id", self.tasks.answer)

    def archive(self, task_id: str, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        return self._mutation(task_id, request, auth_context, "archive", "idempotency_key", self.tasks.archive)

    def list_device_profiles(self, origin: dict[str, Any] | None, *, auth_context: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]:
        port = self._profile_port()
        principal_id, controller_id = self._owner(auth_context)
        result = self._call_port(port.list, {"principal_id": principal_id, "controller_id": controller_id, "cursor": cursor, "limit": self._limit(limit)})
        page = self._safe_page(result, "profiles")
        return {"items": page["profiles"], "next_cursor": page["next_cursor"]}

    def discover_emulators(self, *, auth_context: dict[str, Any]) -> dict[str, Any]:
        port = self._profile_port()
        principal_id, controller_id = self._owner(auth_context)
        result = self._call_port(port.discover_emulators, {
            "principal_id": principal_id, "controller_id": controller_id,
        })
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ExecutionContractError(
                "DEVICE_PROFILE_PORT_INVALID", "The emulator discovery result is invalid.", 503
            )
        return _redact(result, max_bytes=self.max_event_payload_bytes)

    def get_device_profile(self, profile_id: str, origin: dict[str, Any] | None, *, auth_context: dict[str, Any]) -> dict[str, Any]:
        port = self._profile_port()
        principal_id, controller_id = self._owner(auth_context)
        profile = self._call_port(
            port.get, profile_id, {"principal_id": principal_id, "controller_id": controller_id}
        )
        if profile is None:
            raise ExecutionContractError("DEVICE_PROFILE_NOT_FOUND", "The device profile was not found.", 404)
        return self._safe_profile(profile)

    def create_device_profile(self, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        port = self._profile_port()
        principal_id, controller_id = self._owner(auth_context)
        return self._safe_profile(self._call_port(port.create, {
            "principal_id": principal_id, "controller_id": controller_id, "profile": request["profile"],
            "idempotency_key": request["idempotency_key"],
        }))

    def update_device_profile(self, profile_id: str, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        port = self._profile_port()
        principal_id, controller_id = self._owner(auth_context)
        return self._safe_profile(self._call_port(port.update, profile_id, {
            "principal_id": principal_id, "controller_id": controller_id, "profile": request["profile"],
            "idempotency_key": request["idempotency_key"],
        }))

    def verify_device_profile(self, profile_id: str, request: dict[str, Any], *, auth_context: dict[str, Any]) -> dict[str, Any]:
        port = self._profile_port()
        principal_id, controller_id = self._owner(auth_context)
        return self._safe_profile(self._call_port(port.verify, profile_id, {
            "principal_id": principal_id, "controller_id": controller_id, "idempotency_key": request["idempotency_key"],
        }))

    def experience(self, task_id: str, origin: dict[str, Any] | None, *, auth_context: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]:
        self._owned_alias(task_id, auth_context)
        if self.experiences is None:
            raise ExecutionContractError("EXPERIENCE_UNAVAILABLE", "Task experience is unavailable.", 503)
        result = self._call_port(self.experiences.list_for_task, task_id, cursor=cursor, limit=self._limit(limit))
        return self._safe_page(result, "items")

    def frame_metadata(self, task_id: str, *, auth_context: dict[str, Any]) -> dict[str, Any]:
        self._owned_alias(task_id, auth_context)
        port = self._frame_port()
        principal_id, controller_id = self._owner(auth_context)
        result = self._call_port(port.metadata, task_id, {
            "principal_id": principal_id, "controller_id": controller_id,
        })
        if not isinstance(result, dict) or result.get("content_type") != "image/png":
            raise ExecutionContractError("FRAME_PORT_INVALID", "The verified frame is invalid.", 503)
        return _redact(result, max_bytes=self.max_event_payload_bytes)

    def frame_content(
        self, task_id: str, frame_id: str, *, auth_context: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        self._owned_alias(task_id, auth_context)
        port = self._frame_port()
        principal_id, controller_id = self._owner(auth_context)
        result = self._call_port(port.content, task_id, frame_id, {
            "principal_id": principal_id, "controller_id": controller_id,
        })
        if (
            not isinstance(result, tuple) or len(result) != 2
            or not isinstance(result[0], dict) or not isinstance(result[1], bytes)
            or result[0].get("content_type") != "image/png"
        ):
            raise ExecutionContractError("FRAME_PORT_INVALID", "The verified frame is invalid.", 503)
        return result

    def _mutation(self, task_id: str, request: dict[str, Any], auth_context: dict[str, Any], kind: str, operation_field: str, method: Any) -> dict[str, Any]:
        alias = self._owned_alias(task_id, auth_context)
        principal_id, controller_id = self._owner(auth_context)
        operation_key = str(request.get("idempotency_key") or request[operation_field])
        # include the semantic ID as well, so one idempotency key cannot silently
        # be replayed for a different control/revision/question.
        digest = _sha(_json({key: value for key, value in request.items() if key != "origin"}))
        try:
            operation, created = self.store.reserve_v2_operation(
                task_id=task_id, operation_key=operation_key, operation_kind=kind,
                payload_hash=digest, principal_id=principal_id, controller_id=controller_id, created_at=_now(),
            )
        except ValueError:
            raise ExecutionContractError("OPERATION_IDEMPOTENCY_CONFLICT", "The operation key was already used with different input.", 409) from None
        previous = self.store.v2_operation_result(operation)
        if not created and previous is not None:
            return previous
        port_request = {key: value for key, value in request.items() if key != "origin"}
        port_request.update({"operation_id": f"{kind}:{task_id}:{operation_key}", "principal_id": principal_id, "controller_id": controller_id})
        result = self._call_port(method, task_id, port_request)
        safe = self._safe_task(result)
        self.store.complete_v2_operation(task_id=task_id, operation_key=operation_key, result=safe, applied_at=_now())
        return safe

    def _owned_alias(self, task_id: str, auth_context: dict[str, Any]) -> dict[str, Any]:
        alias = self.store.v2_alias_for_task(task_id)
        principal_id, controller_id = self._owner(auth_context)
        if alias is None or not (
            hmac.compare_digest(str(alias["principal_id"]), principal_id)
            and hmac.compare_digest(str(alias["controller_id"]), controller_id)
        ):
            # Do not expose another local capability principal's task.
            raise ExecutionContractError("TASK_NOT_FOUND", "The task was not found.", 404)
        return alias

    def _task_response(self, task_id: str, execution_id: str, *, replayed: bool = False, projection: dict[str, Any] | None = None) -> dict[str, Any]:
        task = projection if projection is not None else self._call_port(self.tasks.get, task_id)
        if task is None:
            raise ExecutionContractError("TASK_RECOVERY_PENDING", "The task is recovering its canonical projection.", 503)
        response = self._safe_task(task)
        if str(response.get("task_id")) != task_id:
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned an identity mismatch.", 503)
        response["execution_id"] = execution_id
        response["replayed"] = replayed
        return response

    @staticmethod
    def _origin(identity: dict[str, Any], execution_id: str) -> dict[str, Any]:
        return {
            "dsh_session_id": identity["dsh_session_id"], "created_execution_id": execution_id,
            "created_turn_id": str(identity["dsh_turn_id"]), "created_tool_call_id": identity["tool_call_id"],
            "root_call_id": identity["root_call_id"],
        }

    @staticmethod
    def _owner(auth_context: dict[str, Any]) -> tuple[str, str]:
        principal_id = auth_context.get("principal_id")
        controller_id = auth_context.get("controller_id")
        if (
            not isinstance(principal_id, str) or not principal_id
            or not isinstance(controller_id, str) or not controller_id
        ):
            raise ExecutionContractError(
                "EXECUTION_CLIENT_UNAUTHORIZED",
                "The local execution client is not authorized.", 403,
            )
        return principal_id, controller_id

    @staticmethod
    def _required_task_id(projection: dict[str, Any]) -> str:
        value = projection.get("task_id") if isinstance(projection, dict) else None
        if not isinstance(value, str) or not value:
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime did not create a stable task identity.", 503)
        return value

    @staticmethod
    def _validated_status(status: str | None) -> None:
        if status is not None and status not in TASK_STATUSES:
            raise ExecutionContractError("TASK_STATUS_UNSUPPORTED", "The requested task status is unsupported.", 409)

    def _limit(self, limit: int) -> int:
        if not isinstance(limit, int) or limit < 1 or limit > self.max_page_size:
            raise ExecutionContractError("PAGE_LIMIT_INVALID", "The page limit is unsupported.", 409)
        return limit

    @staticmethod
    def _safe_cursor(value: Any) -> str | None:
        return value if isinstance(value, str) and len(value) <= 512 else None

    @staticmethod
    def _task_is_allowed(task: Any, allowed: list[str]) -> bool:
        return isinstance(task, dict) and isinstance(task.get("task_id"), str) and task["task_id"] in allowed

    def _safe_task(self, task: Any) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned an invalid task.", 503)
        task_id = task.get("task_id")
        status = task.get("status")
        if not isinstance(task_id, str) or not task_id or status not in TASK_STATUSES:
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned an unsupported task projection.", 503)
        return _redact(task, max_bytes=self.max_event_payload_bytes)

    def _safe_event(self, event: Any) -> dict[str, Any]:
        if not isinstance(event, dict) or not isinstance(event.get("cursor"), int) or event["cursor"] < 0:
            raise ExecutionContractError("TASK_PORT_INVALID", "The Task runtime returned an invalid event.", 503)
        safe = _redact(event, max_bytes=self.max_event_payload_bytes)
        if len(_json(safe).encode("utf-8")) > self.max_event_payload_bytes:
            return {
                "cursor": event["cursor"],
                "type": str(event.get("type") or "runtime.event")[:128],
                "payload": {"truncated": True},
            }
        return safe

    def _safe_page(self, page: Any, items_key: str) -> dict[str, Any]:
        if not isinstance(page, dict):
            raise ExecutionContractError("TASK_PORT_INVALID", "The runtime returned an invalid page.", 503)
        items = page.get(items_key, [])
        if not isinstance(items, list) or len(items) > self.max_page_size:
            raise ExecutionContractError("TASK_PORT_INVALID", "The runtime returned an invalid page.", 503)
        return {items_key: [_redact(item, max_bytes=self.max_event_payload_bytes) for item in items], "next_cursor": self._safe_cursor(page.get("next_cursor"))}

    def _safe_profile(self, profile: Any) -> dict[str, Any]:
        if not isinstance(profile, dict) or not isinstance(profile.get("device_profile_id"), str):
            raise ExecutionContractError("DEVICE_PROFILE_PORT_INVALID", "The device profile runtime returned an invalid projection.", 503)
        return _redact(profile, max_bytes=self.max_event_payload_bytes)

    def _profile_port(self) -> V2DeviceProfilePort:
        if self.device_profiles is None:
            raise ExecutionContractError("DEVICE_PROFILE_UNAVAILABLE", "Device profiles are unavailable.", 503)
        return self.device_profiles

    def _frame_port(self) -> V2FramePort:
        if self.frames is None:
            raise ExecutionContractError("FRAME_UNAVAILABLE", "The verified frame is unavailable.", 503)
        return self.frames

    @staticmethod
    def _call_port(method: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except ExecutionContractError:
            raise
        except Exception:
            # Raw port errors often contain paths, transport details, or secrets.
            raise ExecutionContractError("TASK_PORT_UNAVAILABLE", "The Task runtime is unavailable.", 503) from None


_REDACTED_KEYS = frozenset({
    "token", "secret", "password", "authorization", "cookie", "credential",
    "command_line", "commandline", "raw_command", "adb_serial", "serial", "ui_tree",
    "screenshot", "screenshot_bytes", "artifact_path", "path", "properties",
})
_INTERNAL_OWNER_KEYS = frozenset({
    "principal_id", "controller_id", "runner_kind", "runner_version",
    "canonical_envelope_json", "canonical_create_envelope", "envelope_hash",
    "request_hash", "submission_key", "canonical_client_request_id",
    "canonical_idempotency_key", "authorization_mode",
})


def _safe_create_goal(value: Any) -> dict[str, Any]:
    """Keep retry material useful while refusing credential-shaped payloads."""
    source = value if isinstance(value, dict) else {}
    return {
        "summary": sanitize_task_goal(source.get("summary"), maximum=1500),
        "task_kind": sanitize_task_goal(source.get("task_kind") or "bounded", maximum=32),
        "stop_condition": sanitize_task_payload(
            source.get("stop_condition") or {}, maximum_string=1500, maximum_items=30,
        ),
        "schedule": sanitize_task_payload(
            source.get("schedule"), maximum_string=1500, maximum_items=30,
        ),
    }


def _redact(value: Any, *, max_bytes: int, depth: int = 0) -> Any:
    """Bound public projections without copying raw device/credential material."""
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                result["truncated"] = True
                break
            key_text = str(key)
            if key_text.lower() in _INTERNAL_OWNER_KEYS:
                # The capability owner is port-only authorization context.
                # It must not become a public task/event/error projection.
                continue
            if any(part in key_text.lower() for part in _REDACTED_KEYS):
                result[key_text] = "[redacted]"
            elif key_text.lower() in {"error", "last_error"}:
                result[key_text] = _safe_error_projection(item)
            else:
                result[key_text[:128]] = _redact(item, max_bytes=max_bytes, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item, max_bytes=max_bytes, depth=depth + 1) for item in value[:50]]
    if isinstance(value, bytes):
        return "[redacted]"
    if isinstance(value, str):
        return value[: min(4096, max_bytes)]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1024]


def _safe_error_projection(value: Any) -> dict[str, str] | None:
    """Preserve no-error truth and expose only a stable, validator-safe error."""
    if value is None:
        return None
    raw_code = str(value.get("code") or "TASK_ERROR") if isinstance(value, dict) else "TASK_ERROR"
    code = "".join(
        character if character.isascii() and (character.isalnum() or character in "._:-") else "_"
        for character in raw_code
    )[:128].strip("_") or "TASK_ERROR"
    return {"code": code, "summary": "The Task runtime reported an error."}


__all__ = [
    "ExecutionContractError", "ExecutionContractService", "STATUSES",
    "V2DeviceProfilePort", "V2ExecutionContractService", "V2ExperiencePort", "V2FramePort",
    "RunnerAdmissionPort", "V2TaskCreateRequest", "V2TaskPort",
]
