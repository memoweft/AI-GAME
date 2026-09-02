from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .preflight import PreflightResult
from .repair import GoalRepairManager, RepairApplyError


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


class ProductionGoalRepairs:
    """Registers bounded U2 repairs for installed, locally owned components."""

    def __init__(
        self, *, manager: GoalRepairManager, project_root: Path,
        capability_snapshot: Callable[[], dict[str, Any]],
        discover_targets: Callable[[], Any], adb_path: str | Path | None,
        model_control_script: str | Path | None = None,
        repository_model_enabled: bool = True,
        runner: CommandRunner | None = None,
    ) -> None:
        self.manager = manager
        self.project_root = project_root.resolve()
        self.capability_snapshot = capability_snapshot
        self.discover_targets = discover_targets
        self.adb_path = Path(adb_path).resolve() if adb_path else None
        self.model_control_script = (
            Path(model_control_script).resolve() if model_control_script else None
        )
        self.repository_model_enabled = repository_model_enabled
        self.runner = runner or self._run

    def attempt(self, goal_id: str, result: PreflightResult) -> bool:
        attempted = False
        if self._model_needs_repair(result):
            attempted = self._attempt_model(goal_id) or attempted
        if self._android_needs_repair(result):
            attempted = self._attempt_mumu(goal_id) or attempted
        return attempted

    def _attempt_model(self, goal_id: str) -> bool:
        script = self.model_control_script
        if script is None and self.repository_model_enabled:
            script = self.project_root / "scripts" / "model-runtime.ps1"
        shell = self._powershell()
        if shell is None or script is None or not script.is_file():
            return False
        status = self._model_status(shell, script)
        # Missing configuration or installation is not repaired by this
        # reversible startup path.
        if status.get("status") not in {"stopped", "stale_pid", "pid_mismatch", "running"}:
            return False
        repair_key = self._retryable_key(goal_id, "model-start:" + _fingerprint({
            "status": status.get("status"),
            "pid": status.get("pid"),
            "model_id": status.get("model_id"),
            "gpu_free_mib": status.get("gpu_free_mib"),
        }))

        def identity() -> dict[str, Any]:
            current = self._model_status(shell, script)
            state = current.get("status")
            confirmed = state in {"stopped", "stale_pid"}
            return {
                "confirmed": confirmed,
                "owner": "AI-GAME",
                "status": state or "unknown",
                "pid": current.get("pid") or None,
                "reason": (
                    "owned service is stopped and may be started"
                    if confirmed else
                    "existing or mismatched process cannot be started over safely"
                ),
            }

        def apply() -> dict[str, Any]:
            completed = self.runner(
                self._powershell_command(shell, script, "start"), 390.0
            )
            if completed.returncode != 0:
                # Exit 3/4 refuse before mutation. Exit 5 is also a no-op when
                # status was cleanly stopped; stale PID cleanup is a write.
                no_mutation = completed.returncode in {3, 4} or (
                    completed.returncode == 5 and status.get("status") == "stopped"
                )
                raise RepairApplyError(
                    f"owned model start helper refused with exit {completed.returncode}",
                    applied=not no_mutation,
                )
            return {"detail": "owned model start helper completed"}

        self.manager.run(
            goal_id, repair_key=repair_key, component="local.visual_grounding",
            readiness_probe=self._model_readiness,
            identity_probe=identity, apply_repair=apply,
        )
        return True

    def _retryable_key(self, goal_id: str, base: str) -> str:
        matching = [
            item for item in self.manager.store.repairs(goal_id)
            if item["repair_key"] == base or item["repair_key"].startswith(base + ":retry:")
        ]
        if not matching:
            return base
        latest = matching[-1]
        if latest["state"] == "FAILED" and latest["applied"] is False:
            return f"{base}:retry:{len(matching)}"
        return str(latest["repair_key"])

    def _attempt_mumu(self, goal_id: str) -> bool:
        script = self.project_root / "scripts" / "sync-mumu-executor.ps1"
        shell = self._powershell()
        cli = self._mumu_cli()
        if shell is None or cli is None or not script.is_file():
            return False
        initial = self._mumu_identity(cli)
        serial = initial.get("serial")
        repair_key = "mumu-sync:" + _fingerprint({
            "serial": serial,
            "state": initial.get("player_state"),
            "confirmed": initial.get("confirmed"),
        })

        def readiness() -> dict[str, Any]:
            discovery = self.discover_targets()
            targets = tuple(getattr(discovery, "targets", ()))
            ready_serials = sorted(
                str(getattr(item, "external_id", ""))
                for item in targets
                if str(getattr(item, "status", "")) == "ready"
            )
            return {
                "ready": bool(serial and serial in ready_serials),
                "expected_serial": serial,
                "ready_serials": ready_serials,
            }

        def identity() -> dict[str, Any]:
            return self._mumu_identity(cli)

        def apply() -> dict[str, Any]:
            completed = self.runner(
                self._powershell_command(shell, script, "-VmIndex", "0"), 30.0
            )
            if completed.returncode != 0:
                raise RuntimeError("MuMu sync helper failed")
            return {"detail": "running MuMu 0 synchronized and ADB reconnected"}

        self.manager.run(
            goal_id, repair_key=repair_key, component="android.mumu_adb",
            readiness_probe=readiness, identity_probe=identity, apply_repair=apply,
        )
        return True

    def _model_readiness(self) -> dict[str, Any]:
        capability = next(
            (item for item in self.capability_snapshot().get("capabilities", ())
             if _value(item, "id") == "model"),
            None,
        )
        return {
            "ready": _value(capability, "status") == "ready",
            "status": str(_value(capability, "status") or "unknown"),
            "detail": str(_value(capability, "detail") or "model state unavailable"),
        }

    def _model_status(self, shell: str, script: Path) -> dict[str, str]:
        try:
            completed = self.runner(
                self._powershell_command(shell, script, "status"), 15.0
            )
        except (OSError, subprocess.SubprocessError):
            return {"status": "probe_error"}
        if completed.returncode != 0:
            return {"status": "probe_error"}
        allowed = {"status", "pid", "model_id", "api_ready", "gpu_free_mib"}
        parsed: dict[str, str] = {}
        for line in (completed.stdout or "").splitlines():
            raw = line.strip()
            key, separator, value = raw.partition("=")
            if not separator:
                key, separator, value = raw.partition(":")
            key = key.strip()
            if separator and key in allowed | {"ready", "listening"}:
                parsed[key] = value.strip()[:256]
        if parsed.get("ready") == "yes":
            parsed["status"] = "running"
            parsed["api_ready"] = "true"
        return parsed

    def _mumu_identity(self, cli: Path) -> dict[str, Any]:
        try:
            completed = self.runner((str(cli), "info", "--vmindex", "0"), 10.0)
            payload = json.loads(completed.stdout or "null") if completed.returncode == 0 else None
            if isinstance(payload, list):
                payload = payload[0] if len(payload) == 1 else None
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return {"confirmed": False, "owner": "external_mumu", "reason": "identity unavailable"}
        host = str(payload.get("adb_host_ip") or "")
        port = str(payload.get("adb_port") or "")
        valid_port = port.isdigit() and 1 <= int(port) <= 65535
        confirmed = bool(
            str(payload.get("index")) == "0"
            and payload.get("is_android_started") is True
            and payload.get("is_process_started") is True
            and str(payload.get("player_state")) == "start_finished"
            and host == "127.0.0.1"
            and valid_port
        )
        return {
            "confirmed": confirmed,
            "owner": "external_mumu",
            "vm_index": 0,
            "player_state": str(payload.get("player_state") or "unknown"),
            "serial": f"127.0.0.1:{port}" if host == "127.0.0.1" and valid_port else None,
            "reason": (
                "already-running MuMu 0 identity confirmed; lifecycle is not changed"
                if confirmed else "MuMu 0 is not an eligible already-running loopback target"
            ),
        }

    def _mumu_cli(self) -> Path | None:
        if self.adb_path is None or not self.adb_path.is_file():
            return None
        try:
            root = self.adb_path.parents[3]
        except IndexError:
            return None
        candidate = root / "nx_main" / "mumu-cli.exe"
        return candidate if candidate.is_file() else None

    @staticmethod
    def _model_needs_repair(result: PreflightResult) -> bool:
        return any(
            item.get("capability") == "local.visual_grounding"
            and item.get("state") != "READY"
            for item in result.facts
        )

    @staticmethod
    def _android_needs_repair(result: PreflightResult) -> bool:
        return not any(
            str(item.get("capability", "")).startswith("target:")
            and item.get("state") == "READY"
            for item in result.facts
        )

    @staticmethod
    def _powershell_command(shell: str, script: Path, *arguments: str) -> tuple[str, ...]:
        return (
            shell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments,
        )

    @staticmethod
    def _powershell() -> str | None:
        return shutil.which("pwsh.exe") or shutil.which("powershell.exe")

    @staticmethod
    def _run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False,
            timeout=timeout, creationflags=creation_flags,
        )


def _fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)
