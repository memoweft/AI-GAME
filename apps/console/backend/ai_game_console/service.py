from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen
from uuid import uuid4

from .adb_executor import AdbGuiExecutor
from .cloud_config import CloudChatConfiguration
from .config import Settings
from .discovery import AdbDiscoveryResult, AdbTargetDiscovery
from .domain import Approval, ApprovalStatus, Run, RunStatus, RuntimeCapability, TargetKind
from .execution import GuiAction, GuiExecutor
from .repository import ConcurrentUpdate, RecordNotFound, SQLiteRepository, utc_now
from .schemas import ApprovalDecisionRequest, ExecutorActionRequest, RunCreate


class ControlPlaneError(RuntimeError):
    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True, slots=True)
class TargetDiscoveryView:
    targets: list[Any]
    discovery: AdbDiscoveryResult


class RuntimeProbe:
    """Cheap, non-blocking capability projection.

    The local model is verified with a short, read-only OpenAI-compatible
    ``/v1/models`` request. The GUI executor performs its own small, explicit
    ADB ``get-state`` check and is never inferred from configuration alone.
    """

    MODEL_PROBE_TIMEOUT_SECONDS = 0.5

    def __init__(
        self,
        settings: Settings,
        adb_discovery: AdbTargetDiscovery,
        env: dict[str, str] | None = None,
        model_transport: Callable[[str, float, str | None], Mapping[str, Any]] | None = None,
        adb_executor: GuiExecutor | None = None,
        cloud_configuration: CloudChatConfiguration | None = None,
    ) -> None:
        self.settings = settings
        self.adb_discovery = adb_discovery
        self.env = os.environ if env is None else env
        self.model_transport = model_transport or self._fetch_model_catalog
        self.adb_executor = adb_executor or AdbGuiExecutor.from_settings(settings)
        self.cloud_configuration = cloud_configuration

    @staticmethod
    def _read_model_runtime_config(path: Path) -> dict[str, str]:
        if not path.is_file():
            return {}
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").lstrip()
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    @staticmethod
    def _fetch_model_catalog(
        url: str, timeout: float, api_key: str | None
    ) -> Mapping[str, Any]:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - local configured endpoint
            payload = response.read().decode("utf-8")
        decoded = json.loads(payload)
        return decoded if isinstance(decoded, Mapping) else {}

    def _model_capability(self) -> RuntimeCapability:
        config_path = self.settings.project_root / "config" / "model-runtime.env"
        config = self._read_model_runtime_config(config_path)
        host = config.get("GUI_MODEL_HOST", "").strip()
        port = config.get("GUI_MODEL_PORT", "").strip()
        served_name = config.get("GUI_MODEL_SERVED_NAME", "").strip()
        if not (host and port and served_name):
            return RuntimeCapability(
                id="model",
                name="GUI 模型运行时",
                status="not_configured",
                configured=False,
                detail="未找到完整的本地 GUI 模型配置。",
                blocker={"code": "model_not_configured", "message": "模型尚未配置。"},
            )

        try:
            port_number = int(port)
            if not 1 <= port_number <= 65535:
                raise ValueError
        except ValueError:
            return RuntimeCapability(
                id="model",
                name="GUI 模型运行时",
                status="not_configured",
                configured=False,
                detail="本地 GUI 模型端口配置无效。",
                blocker={"code": "model_not_configured", "message": "模型尚未配置。"},
            )

        endpoint = f"http://{host}:{port_number}/v1/models"
        try:
            catalog = self.model_transport(
                endpoint,
                self.MODEL_PROBE_TIMEOUT_SECONDS,
                config.get("GUI_MODEL_API_KEY") or None,
            )
            models = catalog.get("data", [])
            model_is_served = isinstance(models, list) and any(
                isinstance(item, Mapping) and item.get("id") == served_name
                for item in models
            )
        except (OSError, ValueError, json.JSONDecodeError):
            model_is_served = False

        if model_is_served:
            return RuntimeCapability(
                id="model",
                name="GUI 模型运行时",
                status="ready",
                configured=True,
                detail=f"本地模型运行时已就绪，正在服务 {served_name}。",
                blocker=None,
            )
        return RuntimeCapability(
            id="model",
            name="GUI 模型运行时",
            status="stopped",
            configured=True,
            detail="模型端点未就绪，或未服务已配置的 GUI 模型。",
            blocker={"code": "model_runtime_stopped", "message": "模型运行时尚未启动。"},
        )

    def snapshot(self) -> dict[str, Any]:
        model = self._model_capability()

        if self.cloud_configuration is not None:
            cloud = self.cloud_configuration.runtime_capability()
        else:
            cloud_configured = bool(
                self.settings.cloud_chat_endpoint
                and self.settings.cloud_chat_model
                and self.settings.cloud_chat_api_key
            )
            cloud = RuntimeCapability(
                id="planner",
                name="云端规划器",
                status="unknown" if cloud_configured else "not_configured",
                configured=cloud_configured,
                detail=(
                    "配置已加载；首次发送时验证连接。"
                    if cloud_configured
                    else "云端端点、模型或 API key 尚未完整配置。"
                ),
                blocker=(
                    None
                    if cloud_configured
                    else {
                        "code": "cloud_planner_not_configured",
                        "message": "云端规划器尚未配置。",
                    }
                ),
            )

        # Runtime truth must not be promoted merely because an injected adapter
        # happens to report ready: physical execution is opt-in through these
        # three explicit process settings.
        if not self.settings.gui_executor_enabled:
            executor_probe = AdbGuiExecutor.from_settings(self.settings).probe()
        elif not self.settings.adb_path or not self.settings.adb_serial:
            executor_probe = AdbGuiExecutor.from_settings(self.settings).probe()
        else:
            executor_probe = self.adb_executor.probe()
        executor = RuntimeCapability(
            id="executor",
            name="GUI 执行器",
            status=executor_probe.status,
            configured=executor_probe.configured,
            detail=executor_probe.detail,
            blocker=executor_probe.blocker,
        )

        adb_path = self.adb_discovery.resolve_adb_path()
        adb = RuntimeCapability(
            id="adb",
            name="Android 调试桥",
            status="ready" if adb_path else "not_configured",
            configured=adb_path is not None,
            detail=(
                f"可通过 {adb_path} 进行只读目标发现。"
                if adb_path
                else "未在 AI_GAME_ADB_PATH 或 PATH 中找到 ADB。"
            ),
            blocker=(
                None
                if adb_path
                else {"code": "adb_not_configured", "message": "ADB 尚未配置。"}
            ),
        )

        capabilities = [model, cloud, executor, adb]
        # Optional future integrations do not degrade the independently usable
        # local console. Their truthful state remains visible per capability.
        return {"overall_status": "ready", "capabilities": capabilities}


class ControlPlaneService:
    """Business-state boundary used by every transport route."""

    def __init__(
        self,
        repository: SQLiteRepository,
        settings: Settings,
        adb_discovery: AdbTargetDiscovery | None = None,
        runtime_probe: RuntimeProbe | None = None,
        adb_executor: GuiExecutor | None = None,
        cloud_configuration: CloudChatConfiguration | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.adb_discovery = adb_discovery or AdbTargetDiscovery()
        self.adb_executor = adb_executor or AdbGuiExecutor.from_settings(settings)
        self.runtime_probe = runtime_probe or RuntimeProbe(
            settings,
            self.adb_discovery,
            adb_executor=self.adb_executor,
            cloud_configuration=cloud_configuration,
        )

    def initialize(self) -> None:
        self.repository.initialize()

    def health(self) -> dict[str, str]:
        database = "ready" if self.repository.ping() else "unavailable"
        return {
            "status": "ok" if database == "ready" else "degraded",
            "service": "ai-game-console",
            "version": "0.1.0",
            "database": database,
        }

    def overview(self) -> dict[str, Any]:
        counts = self.repository.overview_counts()
        return {
            "summary": {
                "workflow_count": counts["workflow_count"],
                "target_count": counts["target_count"],
                "active_run_count": counts["active_run_count"],
                "pending_approval_count": counts["pending_approval_count"],
            },
            "run_status_counts": counts["run_status_counts"],
            "recent_runs": self.repository.list_runs(limit=10),
            "runtime": self.runtime(),
        }

    def list_workflows(self):
        return self.repository.list_workflows()

    def list_targets(self):
        return self.repository.list_targets()

    def discover_targets(self) -> TargetDiscoveryView:
        discovery = self.adb_discovery.discover()
        targets = self.repository.replace_adb_targets(discovery.targets)
        self.repository.append_event(
            event_type="targets_discovered",
            message=discovery.message,
            level="info" if discovery.status in {"ready", "not_configured"} else "warning",
            data={
                "adb_status": discovery.status,
                "device_count": len(discovery.devices),
            },
        )
        return TargetDiscoveryView(targets=targets, discovery=discovery)

    def list_runs(self, *, limit: int = 100):
        return self.repository.list_runs(limit=limit)

    def get_run(self, run_id: str) -> Run:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ControlPlaneError(
                code="run_not_found",
                message=f"未找到运行 {run_id!r}。",
                status_code=404,
            )
        return run

    def create_run(self, request: RunCreate) -> Run:
        workflow = self.repository.get_workflow(request.workflow_id)
        if workflow is None:
            raise ControlPlaneError(
                code="workflow_not_found",
                message=f"未找到工作流 {request.workflow_id!r}。",
                status_code=404,
            )
        if not workflow.enabled:
            raise ControlPlaneError(
                code="workflow_disabled",
                message=f"工作流“{workflow.name}”当前可见但尚未启用。",
                status_code=409,
            )

        target = self.repository.get_target(request.target_id)
        if target is None:
            raise ControlPlaneError(
                code="target_not_found",
                message=f"未找到目标 {request.target_id!r}。",
                status_code=404,
            )
        if target.kind is not workflow.target_kind:
            raise ControlPlaneError(
                code="target_kind_mismatch",
                message=(
                    f"工作流 {workflow.id!r} 需要 {workflow.target_kind.value} 类型的目标。"
                ),
                status_code=409,
            )
        if target.status != "ready":
            raise ControlPlaneError(
                code="target_not_ready",
                message=f"目标“{target.name}”当前状态为 {target.status}，尚未就绪。",
                status_code=409,
            )

        now = utc_now()
        run_id = str(uuid4())
        approval_required = bool(request.requires_approval or workflow.requires_approval)
        status = (
            RunStatus.AWAITING_APPROVAL if approval_required else RunStatus.QUEUED
        )
        blocker = (
            "approval_required"
            if approval_required
            else "workflow_executor_not_connected"
        )
        run = Run(
            id=run_id,
            name=request.name or workflow.name,
            workflow_id=workflow.id,
            target_id=target.id,
            instruction=request.instruction,
            exact_text=request.exact_text,
            requires_approval=approval_required,
            status=status,
            blocker=blocker,
            workflow_name=workflow.name,
            target_name=target.name,
            created_at=now,
            updated_at=now,
        )
        approval = None
        if approval_required:
            approval = Approval(
                id=str(uuid4()),
                run_id=run_id,
                status=ApprovalStatus.PENDING,
                note=None,
                created_at=now,
                decided_at=None,
                updated_at=now,
            )
        return self.repository.create_run(run, approval)

    def act_on_run(self, run_id: str, action: str) -> Run:
        run = self.get_run(run_id)
        transitions: dict[str, dict[RunStatus, tuple[RunStatus, str | None]]] = {
            "pause": {
                RunStatus.QUEUED: (
                    RunStatus.PAUSED,
                    "workflow_executor_not_connected",
                ),
                RunStatus.RUNNING: (RunStatus.PAUSED, None),
            },
            "resume": {
                RunStatus.PAUSED: (
                    RunStatus.QUEUED,
                    "workflow_executor_not_connected",
                ),
            },
            "cancel": {
                RunStatus.AWAITING_APPROVAL: (RunStatus.CANCELLED, None),
                RunStatus.QUEUED: (RunStatus.CANCELLED, None),
                RunStatus.RUNNING: (RunStatus.CANCELLED, None),
                RunStatus.PAUSED: (RunStatus.CANCELLED, None),
            },
        }
        transition = transitions[action].get(run.status)
        if transition is None:
            raise ControlPlaneError(
                code="invalid_run_transition",
                message=f"运行处于 {run.status.value} 状态，不能执行 {action}。",
                status_code=409,
            )
        new_status, blocker = transition
        try:
            return self.repository.transition_run(
                run_id=run_id,
                expected_status=run.status,
                new_status=new_status,
                blocker=blocker,
                event_type={
                    "pause": "run_paused",
                    "resume": "run_resumed",
                    "cancel": "run_cancelled",
                }[action],
                message={
                    "pause": "运行已暂停。",
                    "resume": "运行已恢复排队。",
                    "cancel": "运行已取消。",
                }[action],
            )
        except RecordNotFound:
            return self.get_run(run_id)
        except ConcurrentUpdate as exc:
            raise ControlPlaneError(
                code="run_changed",
                message="运行状态已在别处变化，请刷新后重试。",
                status_code=409,
            ) from exc

    def list_approvals(self):
        return self.repository.list_approvals()

    def decide_approval(
        self, approval_id: str, request: ApprovalDecisionRequest
    ) -> tuple[Approval, Run]:
        decision = ApprovalStatus(request.decision)
        try:
            return self.repository.decide_approval(
                approval_id=approval_id,
                decision=decision,
                note=request.note,
            )
        except RecordNotFound as exc:
            raise ControlPlaneError(
                code="approval_not_found",
                message=f"未找到审批 {approval_id!r}。",
                status_code=404,
            ) from exc
        except ConcurrentUpdate as exc:
            raise ControlPlaneError(
                code="approval_already_decided",
                message="该审批已不再处于待处理状态。",
                status_code=409,
            ) from exc

    def execute_adb_action(self, request: ExecutorActionRequest) -> dict[str, str]:
        """Transport one explicitly permitted action without touching runs.

        A successful return only means the local ADB process accepted the
        input command. It does not observe an Android UI result and therefore
        must not complete, start, or otherwise mutate a workflow run.
        """

        if (
            not self.settings.gui_executor_enabled
            or not self.settings.adb_path
            or not self.settings.adb_serial
        ):
            raise ControlPlaneError(
                code="executor_not_configured",
                message="GUI 执行器尚未完成显式 ADB 配置。",
                status_code=409,
            )
        probe = self.adb_executor.probe()
        if not probe.ready:
            blocker = probe.blocker or {
                "code": "executor_not_ready",
                "message": "GUI 执行器尚未就绪。",
            }
            raise ControlPlaneError(
                code=blocker["code"],
                message=blocker["message"],
                status_code=409,
            )

        target = self.repository.get_target(request.target_id)
        if target is None:
            raise ControlPlaneError(
                code="target_not_found",
                message=f"未找到目标 {request.target_id!r}。",
                status_code=404,
            )
        if target.kind is not TargetKind.ANDROID:
            raise ControlPlaneError(
                code="executor_target_kind_mismatch",
                message="受限 ADB 执行仅支持当前就绪的 Android 目标。",
                status_code=409,
            )
        if target.status != "ready":
            raise ControlPlaneError(
                code="target_not_ready",
                message="目标当前未处于 ready 状态。",
                status_code=409,
            )
        if target.external_id != self.settings.adb_serial:
            raise ControlPlaneError(
                code="executor_target_mismatch",
                message="目标不等于当前配置的 ADB 序列号。",
                status_code=409,
            )

        action = GuiAction(
            target_id=request.target_id,
            action=request.action,
            x=request.x,
            y=request.y,
            keycode=request.keycode,
            text=request.text,
        )
        try:
            result = self.adb_executor.execute(action)
        except RuntimeError as exc:
            code = str(exc)
            known_codes = {
                "executor_action_timeout": ("执行器动作超时。", 409),
                "executor_action_unavailable": ("执行器动作暂时不可用。", 409),
                "executor_action_rejected": ("ADB 未接受该动作。", 409),
            }
            message, status_code = known_codes.get(
                code,
                ("执行器未能传输该动作。", 409),
            )
            raise ControlPlaneError(code=code, message=message, status_code=status_code) from exc

        if not result.accepted:
            raise ControlPlaneError(
                code="executor_action_rejected",
                message="ADB 未接受该动作。",
                status_code=409,
            )
        self.repository.append_event(
            event_type="executor_action_transported",
            message="受限 ADB 单原子动作已由本地传输接受；目标界面结果尚未验证。",
            data={"target_id": target.id, "action": request.action},
        )
        return {
            "target_id": target.id,
            "action": request.action,
            "transport_status": "accepted",
            "detail": result.detail,
        }

    def list_events(self, *, limit: int, run_id: str | None):
        return self.repository.list_events(limit=limit, run_id=run_id)

    def runtime(self) -> dict[str, Any]:
        return self.runtime_probe.snapshot()
