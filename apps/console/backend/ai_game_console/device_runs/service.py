from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Any, Callable
from uuid import uuid4

from ..device_lease import DeviceExecutionLease, DeviceLeaseHandle, TargetBusyError
from ..execution import GuiAction
from ..execution_contract.service import ExecutionContractError
from .store import DeviceRunStore
from .ui_tree import UiTreeProjectionError, compact_ui_tree


class DeviceRunError(ExecutionContractError):
    def as_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "detail": self.message}}


@dataclass
class _LiveRun:
    lease: DeviceLeaseHandle
    operation: Lock = field(default_factory=Lock)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class DeviceRunService:
    """Own device use and command receipts, without a model or task planner."""

    def __init__(
        self, *, store: DeviceRunStore, profiles: Any, device_lease: DeviceExecutionLease,
        executor_for_serial: Callable[[str], Any], observation_for_serial: Callable[[str], Any],
        clock: Callable[[], str] = _now,
    ) -> None:
        self.store, self.profiles, self.device_lease = store, profiles, device_lease
        self.executor_for_serial, self.observation_for_serial = executor_for_serial, observation_for_serial
        self.clock = clock
        self._lock = RLock()
        self._live: dict[str, _LiveRun] = {}
        self.store.recover(clock())

    def create(self, request: dict[str, Any], owner: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            existing = self.store.get(request["run_id"])
            if existing is not None:
                self._owned(request["run_id"], owner)
                if existing["request_hash"] != _hash(request):
                    raise DeviceRunError("RUN_ID_CONFLICT", "This run_id already identifies a different device run.", 409)
                return self._public(existing)
            profile, lease = self._acquire_profile(request["device_profile_id"], owner)
            run = {
                **request, **owner, "request_hash": _hash(request),
                "profile_generation": profile.profile_generation, "transport_serial": profile.transport_serial,
                "status": "active", "requires_observation": True, "updated_at": self.clock(), "reason": None,
            }
            try:
                self.store.save(run)
                self._live[run["run_id"]] = _LiveRun(lease)
            except BaseException:
                lease.release()
                raise
            return self._public(run)

    def inspect(self, run_id: str, owner: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            return self._public(self._owned(run_id, owner))

    def observe(
        self,
        run_id: str,
        owner: dict[str, str],
        *,
        include_screenshot: bool,
        ui_tree_format: str = "xml",
    ) -> dict[str, Any]:
        live, run = self._begin(run_id, owner)
        try:
            provider = self.observation_for_serial(run["transport_serial"])
            raw = provider.capture(run["transport_serial"], include_screenshot=include_screenshot)
            with self._lock:
                current = self._owned(run_id, owner)
                current.update(requires_observation=current["status"] != "active", updated_at=self.clock())
                self.store.save(current)
            screenshot = raw.screenshot
            xml = raw.ui_tree.content.decode("utf-8", errors="replace") if raw.ui_tree.content else None
            ui_tree = self._ui_tree_response(
                content=raw.ui_tree.content,
                xml=xml,
                status=raw.ui_tree.status.value,
                error_code=raw.ui_tree.error_code,
                requested_format=ui_tree_format,
            )
            return {
                "run_id": run_id, "observation_id": uuid4().hex, "captured_at": raw.capture_completed_at,
                "screenshot": ({
                    "mime_type": screenshot.content_type,
                    "base64": base64.b64encode(screenshot.content).decode("ascii"),
                    "width": screenshot.width, "height": screenshot.height,
                } if include_screenshot and screenshot.content else None),
                "ui_tree": ui_tree,
                "device_state": asdict(raw.device_state), "consistency": asdict(raw.consistency),
                "requires_observation": current["requires_observation"],
            }
        except ExecutionContractError:
            raise
        except Exception:
            self._require_observation(run_id)
            raise DeviceRunError("DEVICE_OBSERVATION_FAILED", "Could not read the device. Check its connection and observe again.", 409) from None
        finally:
            self._end(run_id, live)

    def actions(self, run_id: str, owner: dict[str, str], request: dict[str, Any]) -> dict[str, Any]:
        command_id, digest = request["command_id"], _hash(request["actions"])
        with self._lock:
            self._owned(run_id, owner)
            previous = self.store.command(run_id, command_id)
            if previous is not None:
                if previous["request_hash"] != digest:
                    raise DeviceRunError("COMMAND_ID_CONFLICT", "Reuse command_id only for an identical action batch.", 409)
                if previous["result"]["outcome"] == "pending":
                    raise DeviceRunError("DEVICE_OPERATION_IN_PROGRESS", "This command is still running. Wait for its receipt; it will not be sent again.", 409)
                return {**previous["result"], "replayed": True}
            live, run = self._begin(run_id, owner, require_observed=True)
        result = {
            "run_id": run_id, "command_id": command_id, "status": "active", "outcome": "pending",
            "accepted": False, "completed_count": 0, "results": [], "requires_observation": False, "replayed": False,
        }
        try:
            # Claim before any physical input. Persist progress without input text.
            self.store.save_command(run_id, command_id, digest, result)
            executor = self.executor_for_serial(run["transport_serial"])
            for index, payload in enumerate(request["actions"]):
                with self._lock:
                    current = self._owned(run_id, owner)
                    if current["status"] != "active":
                        result.update(outcome="interrupted", status=current["status"])
                        break
                started = time.monotonic()
                try:
                    receipt = executor.execute(GuiAction(target_id=run["transport_serial"], **payload))
                    accepted = bool(receipt.accepted)
                    outcome = "accepted" if accepted else "rejected"
                    detail = receipt.detail
                except ValueError:
                    accepted, outcome, detail = False, "rejected", "The device action parameters were rejected."
                except Exception:
                    accepted, outcome, detail = False, "uncertain", "The device did not return a conclusive receipt. Observe before deciding the next action."
                result["results"].append({"index": index, "action": payload["action"], "accepted": accepted,
                                          "outcome": outcome, "detail": detail, "elapsed_ms": round((time.monotonic()-started)*1000)})
                result["completed_count"] += int(accepted)
                self.store.save_command(run_id, command_id, digest, result)
                if not accepted:
                    result["outcome"] = outcome
                    break
            if result["outcome"] == "pending":
                result.update(outcome="accepted", accepted=True)
            with self._lock:
                current = self._owned(run_id, owner)
                if result["outcome"] != "accepted":
                    current["requires_observation"] = True
                current["updated_at"] = self.clock()
                self.store.save(current)
                result.update(status=current["status"], requires_observation=current["requires_observation"])
                self.store.save_command(run_id, command_id, digest, result)
            return result
        finally:
            self._end(run_id, live)

    def control(self, run_id: str, owner: dict[str, str], action: str) -> dict[str, Any]:
        with self._lock:
            run = self._owned(run_id, owner)
            if run["status"] in {"cancelled", "completed"}:
                if action not in {"cancel", "complete"}:
                    raise DeviceRunError("DEVICE_RUN_TERMINAL", "This device run has ended. Create a new run to continue.", 409)
                return self._public(run)
            live = self._live.get(run_id)
            if action == "resume":
                if run["status"] == "active":
                    return self._public(run)
                if live is not None:
                    raise DeviceRunError("DEVICE_OPERATION_IN_PROGRESS", "The last device operation is finishing. Resume after its receipt arrives.", 409)
                profile, lease = self._acquire_profile(run["device_profile_id"], owner)
                run.update(status="active", transport_serial=profile.transport_serial, profile_generation=profile.profile_generation)
                self._live[run_id] = _LiveRun(lease)
            else:
                run["status"] = {"pause": "paused", "cancel": "cancelled", "complete": "completed"}[action]
                if live is not None and not live.operation.locked():
                    live.lease.release()
                    del self._live[run_id]
            run.update(requires_observation=True, reason=None, updated_at=self.clock())
            self.store.save(run)
            return self._public(run)

    def _acquire_profile(self, profile_id: str, owner: dict[str, str]):
        lease = None
        try:
            profile = self.profiles.require_profile(profile_id=profile_id, **owner)
            lease = self.device_lease.require(profile.transport_serial)
            verified = self.profiles.resolve_ready(profile_id=profile_id, **owner)
            if verified.transport_serial != profile.transport_serial:
                raise DeviceRunError("DEVICE_PROFILE_CHANGED", "The device connection changed. Create or resume the run again.", 409)
            return verified, lease
        except TargetBusyError:
            raise DeviceRunError("TARGET_BUSY", "This device is in use by another run. Pause or finish that run before continuing.", 409) from None
        except ExecutionContractError:
            if lease is not None:
                lease.release()
            raise
        except Exception:
            if lease is not None:
                lease.release()
            raise DeviceRunError("DEVICE_PROFILE_UNAVAILABLE", "This device profile is unavailable. Check the selected device and connection.", 409) from None

    def _owned(self, run_id: str, owner: dict[str, str]) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None or any(run[key] != owner[key] for key in ("principal_id", "controller_id")):
            raise DeviceRunError("DEVICE_RUN_NOT_FOUND", "This device run was not found for the current host.", 404)
        return run

    def _begin(self, run_id: str, owner: dict[str, str], *, require_observed: bool = False):
        with self._lock:
            run = self._owned(run_id, owner)
            live = self._live.get(run_id)
            if run["status"] != "active" or live is None:
                raise DeviceRunError("DEVICE_RUN_NOT_ACTIVE", "Resume this device run before using it.", 409)
            if require_observed and run["requires_observation"]:
                raise DeviceRunError("FRESH_OBSERVATION_REQUIRED", "Observe the current device before sending actions.", 409)
            if not live.operation.acquire(blocking=False):
                raise DeviceRunError("DEVICE_OPERATION_IN_PROGRESS", "Wait for this run's current operation before sending another.", 409)
            return live, run

    def _end(self, run_id: str, live: _LiveRun) -> None:
        with self._lock:
            live.operation.release()
            run = self.store.get(run_id)
            if run is None or run["status"] != "active":
                live.lease.release()
                self._live.pop(run_id, None)

    def _require_observation(self, run_id: str) -> None:
        with self._lock:
            run = self.store.get(run_id)
            if run is not None:
                run.update(requires_observation=True, updated_at=self.clock())
                self.store.save(run)

    @staticmethod
    def _ui_tree_response(
        *,
        content: bytes | None,
        xml: str | None,
        status: str,
        error_code: str | None,
        requested_format: str,
    ) -> dict[str, Any]:
        if requested_format == "xml":
            return {"format": "xml", "xml": xml, "status": status, "error_code": error_code}
        if requested_format != "compact":
            raise ValueError("unsupported ui_tree_format")
        if content is None:
            # Preserve the unavailable/failed source state.  An empty node list
            # would incorrectly imply a successfully parsed empty screen.
            return {"format": "xml", "xml": None, "status": status, "error_code": error_code,
                    "fallback_reason": "ui_tree_unavailable"}
        try:
            compact = compact_ui_tree(content)
        except UiTreeProjectionError:
            # Never convert malformed XML into an authoritative empty screen.
            return {"format": "xml", "xml": xml, "status": status, "error_code": error_code,
                    "fallback_reason": "compact_parse_failed"}
        return {"format": "compact", "status": status, "error_code": error_code, **compact}

    @staticmethod
    def _public(run: dict[str, Any]) -> dict[str, Any]:
        return {key: run[key] for key in ("run_id", "status", "device_profile_id", "profile_generation", "requires_observation", "updated_at", "reason")}

    def close(self) -> None:
        with self._lock:
            for run_id, live in list(self._live.items()):
                run = self.store.get(run_id)
                if run is not None:
                    run.update(status="paused", requires_observation=True, reason="service_stopped", updated_at=self.clock())
                    self.store.save(run)
                if not live.operation.locked():
                    live.lease.release()
                    del self._live[run_id]
            # FastAPI waits for request completion before lifespan shutdown.
            self.store.close()
