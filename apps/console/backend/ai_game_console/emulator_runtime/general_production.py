"""Production composition for the exact ``android_ui_agent/1`` runner.

This module adapts existing authorities instead of creating another workflow:
AgentRuntime owns Task/control/revision, RuntimeKernel owns observations and
physical Actions, K1 owns command claims, and K2 owns semantic step records.
Constructing these objects performs no discovery, model call, or device I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..agent_runtime.domain import (
    RunnerDispatchRequest,
    SessionNotFound,
    TaskDispatchConflict,
)
from ..agent_runtime.service import CanonicalTaskService
from ..android_ui_runtime.domain import (
    Anchor,
    AndroidUiActionIntent,
    CanonicalSnapshot,
    CriteriaRevision,
    Criterion,
    CriterionVerdict,
    GroundedUiNode,
    ObservationEnvelope,
    ObservationGroundingQuery,
    OwnerBinding,
    OrderedWaypoint,
    TrustedObservationGroundingManifest,
    UiNode,
    criteria_coverage_digest,
    criteria_digest,
    evidence_marker,
    grounding_manifest_digest,
    observation_grounding_query,
    opaque_digest,
    opaque_node_id,
    owner_scope_digest,
)
from ..android_ui_runtime.role import (
    ActionVerificationContext,
    BoundedToolRoleAdapter,
    PlanningContext,
    PrimitiveVerification,
)
from ..android_ui_runtime.runner import (
    AndroidUiAgentV1Handler,
    DispatchReceipt,
    ExperienceHint,
)
from ..android_ui_runtime.sanitizer import sanitize_summary, sanitize_task_goal
from ..android_ui_runtime.semantic_verification import EvidenceBoundSemanticVerifier
from ..android_ui_runtime.store import CheckpointBaselineQuery, SQLiteAndroidUiStepStore
from ..execution import AndroidScreenshot
from ..experience_runtime import (
    AuthenticatedAndroidScope,
    ExperienceService,
    TrustedVerificationAttestation,
    TrustedVerificationQuery,
    authenticated_android_scope,
)
from ..mobile_agent.domain import Observation as ModelObservation
from ..runtime_kernel import ActionStatus, ActionType, RecordNotFound, RuntimeKernel
from ..runtime_kernel.stage import StageStatus
from ..runtime_kernel.task import TaskSource
from ..runtime_kernel.verify import VerificationMethod, VerificationVerdict
from .general_kernel import (
    GenericDispatchResult,
    GenericEvidenceLookup,
    GenericKernelCommandPort,
    GenericPhysicalDispatcher,
    GenericProfileBinding,
    GenericReconciliationEvidence,
)
from .general_store import (
    GenericCommandClaim,
    GenericCommandConflict,
    SQLiteGenericCommandStore,
    command_payload_digest,
)


RUNNER_KIND = "android_ui_agent"
RUNNER_VERSION = 1
_MODEL_CRITERIA_LIMIT = 8
_UI_TREE_LIMIT = 2 * 1024 * 1024
_UI_NODE_LIMIT = 128
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_ANDROID_PAGE_TITLE_RESOURCES = {"collapsing_toolbar"}
_ANDROID_PACKAGE = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
)
_SHA256 = re.compile(r"[a-f0-9]{64}")
_OBSERVATION_SCHEMA = "runtime-kernel-observation-v1"
_GOAL_CLAUSE_DELIMITER = re.compile(
    r"[，,。；;！？!?\n]+|之后再|后再|然后|并且|以及|同时|接着|最后|并|且|\b(?:and|then|also)\b",
    re.IGNORECASE,
)
_LEADING_ACTION_PREFIXES_ZH = tuple(sorted({
    "请切换到", "请打开", "请进入", "请前往", "请查看", "请找到",
    "再次点击进入", "再次进入", "再进入", "点击进入",
    "请返回到", "请回到", "返回到", "回到", "返回", "先", "再",
    "切换到", "打开", "进入", "前往", "查看", "找到",
}, key=len, reverse=True))
_LEADING_ACTION_PREFIXES_EN = tuple(sorted({
    "please navigate to", "please switch to", "please go to", "please open",
    "please enter", "please view", "please find", "navigate to", "switch to",
    "go to", "open", "enter", "view", "find", "from",
}, key=len, reverse=True))
_GENERIC_DEVICE_TARGET_PREFIXES_ZH = (
    # A navigation goal may name the phone as the place where the UI lives;
    # that generic locator is not part of the page/entity identity. Keep this
    # allowlist deliberately narrow so application, account, and object names
    # remain mandatory in the terminal marker.
    "手机系统的",
    "手机的",
)
_UNSUPPORTED_STATE_PREFIXES_ZH = tuple(sorted({
    "请启用", "请开启", "请关闭", "请停用", "请禁用", "请设置", "请选择",
    "请点击", "请确认", "启用", "开启", "关闭", "停用", "禁用", "设置",
    "选择", "点击", "确认",
}, key=len, reverse=True))
_UNSUPPORTED_STATE_PREFIXES_EN = tuple(sorted({
    "please turn off", "please turn on", "please enable", "please disable",
    "please uncheck", "please check", "please select", "please click",
    "please tap", "please confirm", "please set", "turn off", "turn on",
    "enable", "disable", "uncheck", "check", "select", "click", "tap",
    "confirm", "set",
}, key=len, reverse=True))
_TRAILING_UI_NOUNS_ZH = tuple(sorted({
    "设置页面", "设置界面", "设置屏幕", "页面", "界面", "屏幕", "菜单", "面板", "设置",
}, key=len, reverse=True))
_TRAILING_UI_NOUNS_EN = tuple(sorted({
    "settings page", "settings screen", "settings panel", "page", "screen",
    "menu", "panel", "settings",
}, key=len, reverse=True))
_SEMANTIC_EDGE_PUNCTUATION = " \t\r\n,:;.!?，。；：！？()（）[]【】{}"
_TRAILING_SYSTEM_SETTINGS_ROUTE_ZH = re.compile(
    # ``_semantic_target_core`` trims edge punctuation before applying this
    # rule, so the final right parenthesis may already be absent.
    r"\((?:请)?(?:打开|进入|前往)?\s*系统设置\s*(?:→|->|>)\s*[^()]{1,80}\)?\s*$",
    re.IGNORECASE,
)
_TRAILING_SYSTEM_SETTINGS_ROUTE_EN = re.compile(
    r"[（(](?:(?:please\s+)?(?:open|enter|navigate\s+to)\s+)?"
    r"(?:system\s+)?settings\s*(?:→|->|>)\s*[^()（）]{1,80}[)）]?\s*$",
    re.IGNORECASE,
)
_INLINE_ENGLISH_UI_ALIAS = re.compile(
    r"(?<=[\u4e00-\u9fff])[\"”]?\s*[（(][a-z][a-z0-9 _-]{0,40}[）)]"
)
_ROUTE_EXECUTION_PREFIX_ZH = re.compile(
    r"^(?:(?:仅|只)?通过(?:系统)?界面(?:操作)?)",
)


class ProductionAndroidUiError(RuntimeError):
    """Stable internal failure; raw device/model payloads are never surfaced."""


@dataclass(slots=True)
class _CausalityLedger:
    pending: dict[str, tuple[str, str]] = field(default_factory=dict)

    def record(self, task_id: str, command_id: str, finished_at: str) -> None:
        self.pending[task_id] = (command_id, finished_at)

    def command_for(self, task_id: str, captured_at: str) -> str | None:
        value = self.pending.get(task_id)
        if value is None or _as_timestamp(captured_at) <= _as_timestamp(value[1]):
            return None
        return value[0]


def _durable_command_for_observation(
    *, snapshot: CanonicalSnapshot, captured_at: str, kernel: RuntimeKernel,
    claims: SQLiteGenericCommandStore,
) -> str | None:
    """Restore only the latest accepted command's causality after restart."""

    try:
        actions = kernel.list_actions(snapshot.task_id)
        if not actions:
            return None
        action = actions[-1]
        execution = kernel.load_action_execution(action.id)
        claim = claims.get(action.id)
        if (
            action.task_id != snapshot.task_id
            or action.status not in {ActionStatus.EXECUTED, ActionStatus.VERIFIED}
            or execution.action_id != action.id
            or execution.device_id != snapshot.canonical_device_id
            or not execution.accepted
            or claim is None
            or claim.task_id != snapshot.task_id
            or claim.action_id != action.id
            or claim.command_id != action.id
            or claim.canonical_device_id != snapshot.canonical_device_id
            or claim.owner_principal_id != snapshot.owner.principal_id
            or claim.controller_id != snapshot.owner.controller_id
            or claim.profile_id != snapshot.profile_id
            or claim.profile_generation != snapshot.profile_generation
            or claim.device_boot_id != snapshot.boot_id
            or claim.command_type != action.type.value
            or claim.state != "SETTLED"
            or claim.outcome not in {"accepted", "reconciled_happened"}
            or claim.settled_at is None
            or _as_timestamp(captured_at) <= _as_timestamp(execution.finished_at)
            or _as_timestamp(captured_at) <= _as_timestamp(claim.settled_at)
        ):
            return None
        return action.id
    except Exception:
        return None


@dataclass(slots=True)
class ProductionAndroidCanonicalReader:
    runtime_store: Any
    profiles: Any

    def inspect(self, task_id: str) -> CanonicalSnapshot:
        raw = self.runtime_store.get_task(task_id)
        service = CanonicalTaskService(
            self.runtime_store,
            principal_id=raw.owner_principal_id,
            controller_id=raw.controller_id,
        )
        task = service.inspect_task(task_id)
        bindings = [
            item for item in task.get("subtasks", ())
            if isinstance(item, dict) and item.get("kind") == RUNNER_KIND
        ]
        if len(bindings) != 1:
            raise ProductionAndroidUiError("android_ui_binding_invalid")
        binding = bindings[0]
        profile_id = binding.get("object_ref")
        binding_id = binding.get("subtask_id")
        if not isinstance(profile_id, str) or not isinstance(binding_id, str):
            raise ProductionAndroidUiError("android_ui_binding_invalid")
        profile = self.profiles.require_profile(
            principal_id=raw.owner_principal_id,
            controller_id=raw.controller_id,
            profile_id=profile_id,
        )
        if (
            not profile.enabled
            or getattr(profile.state, "value", profile.state) != "ready"
            or profile.boot_id is None
        ):
            raise ProductionAndroidUiError("android_ui_profile_not_ready")
        return CanonicalSnapshot(
            task_id=task_id,
            owner=OwnerBinding(raw.owner_principal_id, raw.controller_id),
            revision=int(task["current_revision"]),
            status=str(task["status"]),
            allowed_controls=tuple(str(item) for item in task.get("allowed_controls", ())),
            profile_id=profile.profile_id,
            profile_generation=profile.profile_generation,
            boot_id=profile.boot_id,
            canonical_device_id=profile.canonical_device_id,
            runner_kind=RUNNER_KIND,
            runner_version=RUNNER_VERSION,
            runner_binding_id=binding_id,
        )


@dataclass(slots=True)
class ProductionAndroidObservationProvider:
    kernel: RuntimeKernel
    artifacts: Any
    causality: _CausalityLedger
    claims: SQLiteGenericCommandStore | None = None
    _contexts: dict[str, tuple[str, CriteriaRevision]] = field(default_factory=dict)

    def prepare(self, *, task_id: str, goal: str, criteria: CriteriaRevision) -> None:
        self._contexts[task_id] = (sanitize_task_goal(goal), criteria)

    def observe(self, snapshot: CanonicalSnapshot) -> ObservationEnvelope:
        context = self._contexts.get(snapshot.task_id)
        if context is None or context[1].revision != snapshot.revision:
            raise ProductionAndroidUiError("android_ui_observation_context_missing")
        self._ensure_kernel_task(snapshot, *context)
        observation = self.kernel.capture_observation(
            task_id=snapshot.task_id,
            device_id=str(snapshot.canonical_device_id),
        )
        return self._envelope(snapshot, observation)

    def rehydrate(
        self, snapshot: CanonicalSnapshot, durable: Mapping[str, Any],
    ) -> ObservationEnvelope | None:
        """Rebuild one immutable K2 observation without touching the device."""

        try:
            freshness_digest = durable.get("freshness_digest")
            if not isinstance(freshness_digest, str):
                return None
            matching = tuple(
                item for item in self.kernel.observations(snapshot.task_id)
                if opaque_digest("observation-freshness", item.id)
                == freshness_digest
            )
            if len(matching) != 1:
                return None
            source = matching[0]
            screenshot = source.screenshot.artifact
            ui_artifact = source.ui_tree.artifact
            if (
                source.task_id != snapshot.task_id
                or source.device_id != snapshot.canonical_device_id
                or source.screenshot.status.value != "AVAILABLE"
                or source.device_state.status.value != "AVAILABLE"
                or source.consistency.status.value != "consistent"
                or screenshot.sha256 != durable.get("screenshot_digest")
                or _device_state_digest(source.device_state)
                != durable.get("device_state_digest")
                or int(snapshot.profile_generation or 0)
                != durable.get("profile_generation")
                or opaque_digest("profile-id", str(snapshot.profile_id))
                != durable.get("profile_id_digest")
                or opaque_digest("boot-id", str(snapshot.boot_id))
                != durable.get("boot_id_digest")
                or opaque_digest(
                    "canonical-device-id", str(snapshot.canonical_device_id),
                ) != durable.get("canonical_device_id_digest")
            ):
                return None
            if ui_artifact is None:
                if durable.get("ui_tree_digest") is not None:
                    return None
                ui_nodes: tuple[UiNode, ...] = ()
            else:
                if (
                    source.ui_tree.status.value != "AVAILABLE"
                    or ui_artifact.sha256 != durable.get("ui_tree_digest")
                ):
                    return None
                ui_nodes = _parse_ui_tree(
                    self.artifacts.read(ui_artifact),
                    source.device_state.screen_size,
                )
            # Re-read the screenshot too, so a missing or corrupted immutable
            # artifact cannot be converted into recovery authority.
            self.artifacts.read(screenshot)
            state_digest = str(durable["device_state_digest"])
            return ObservationEnvelope(
                task_id=snapshot.task_id,
                profile_id=str(snapshot.profile_id),
                profile_generation=int(snapshot.profile_generation or 0),
                boot_id=str(snapshot.boot_id),
                canonical_device_id=str(snapshot.canonical_device_id),
                screenshot_ref=screenshot.sha256,
                screenshot_digest=screenshot.sha256,
                ui_tree_ref=(ui_artifact.sha256 if ui_artifact is not None else None),
                ui_tree_digest=(ui_artifact.sha256 if ui_artifact is not None else None),
                device_state_ref=state_digest,
                device_state_digest=state_digest,
                observed_at=source.captured_at,
                freshness_token=source.id,
                ui_summary=sanitize_summary(
                    " | ".join(item.text for item in ui_nodes if item.text)[:1_200],
                    maximum=1_200,
                ),
                ui_nodes=ui_nodes,
                causality_command_id=(
                    str(durable["causality_command_id"])
                    if durable.get("causality_command_id") is not None else None
                ),
            )
        except Exception:
            return None

    def _ensure_kernel_task(
        self, snapshot: CanonicalSnapshot, goal: str, criteria: CriteriaRevision,
    ) -> None:
        try:
            task = self.kernel.load_task(snapshot.task_id)
        except RecordNotFound:
            task = self.kernel.create_task(
                task_id=snapshot.task_id,
                goal=goal,
                source=TaskSource(
                    client_id="weftmate-execution-v2",
                    conversation_id=f"task:{snapshot.task_id}",
                    initial_message_id=f"profile:{snapshot.profile_id}",
                ),
                device_id=str(snapshot.canonical_device_id),
            )
        if (
            task.id != snapshot.task_id
            or task.device_id != snapshot.canonical_device_id
            or task.source.client_id != "weftmate-execution-v2"
            or task.source.conversation_id != f"task:{snapshot.task_id}"
            or task.source.initial_message_id != f"profile:{snapshot.profile_id}"
        ):
            raise ProductionAndroidUiError("android_ui_kernel_task_crosswire")
        stages = self.kernel.list_stages(snapshot.task_id)
        stage_id = _stage_id(snapshot.task_id)
        if not stages:
            self.kernel.create_stage(
                task_id=snapshot.task_id,
                stage_id=stage_id,
                objective="Advance one frozen Android UI goal through bounded primitives",
                completion_criteria=tuple(item.description for item in criteria.criteria),
                planner_call_id=f"{RUNNER_KIND}-v{RUNNER_VERSION}",
            )
            stages = self.kernel.list_stages(snapshot.task_id)
        if len(stages) != 1 or stages[0].id != stage_id:
            raise ProductionAndroidUiError("android_ui_kernel_stage_crosswire")
        if stages[0].status is StageStatus.PENDING:
            self.kernel.start_stage(task_id=snapshot.task_id, stage_id=stage_id)

    def _envelope(self, snapshot: CanonicalSnapshot, observation: Any) -> ObservationEnvelope:
        if (
            observation.task_id != snapshot.task_id
            or observation.device_id != snapshot.canonical_device_id
        ):
            raise ProductionAndroidUiError("android_ui_observation_crosswire")
        screenshot = observation.screenshot.artifact
        ui_artifact = observation.ui_tree.artifact
        ui_nodes: tuple[UiNode, ...] = ()
        if ui_artifact is not None:
            ui_nodes = _parse_ui_tree(
                self.artifacts.read(ui_artifact),
                observation.device_state.screen_size,
            )
        state_digest = _device_state_digest(observation.device_state)
        summary = sanitize_summary(
            " | ".join(item.text for item in ui_nodes if item.text)[:1_200],
            maximum=1_200,
        )
        causal_command_id = self.causality.command_for(
            snapshot.task_id, observation.capture_started_at,
        )
        if causal_command_id is None and self.claims is not None:
            causal_command_id = _durable_command_for_observation(
                snapshot=snapshot,
                captured_at=observation.capture_started_at,
                kernel=self.kernel,
                claims=self.claims,
            )
        return ObservationEnvelope(
            task_id=snapshot.task_id,
            profile_id=str(snapshot.profile_id),
            profile_generation=int(snapshot.profile_generation or 0),
            boot_id=str(snapshot.boot_id),
            canonical_device_id=str(snapshot.canonical_device_id),
            screenshot_ref=screenshot.sha256,
            screenshot_digest=screenshot.sha256,
            ui_tree_ref=(ui_artifact.sha256 if ui_artifact is not None else None),
            ui_tree_digest=(ui_artifact.sha256 if ui_artifact is not None else None),
            device_state_ref=state_digest,
            device_state_digest=state_digest,
            observed_at=observation.captured_at,
            freshness_token=observation.id,
            ui_summary=summary,
            ui_nodes=ui_nodes,
            causality_command_id=causal_command_id,
        )


@dataclass(slots=True)
class ProductionRuntimeObservationGrounding:
    """Resolve K2 grounding only from the exact current Kernel artifacts.

    The K2 ledger supplies the identity/digest request, while RuntimeKernel and
    the immutable artifact store supply the independently re-read UI tree.  No
    UI text leaves this adapter: only text-free semantic node facts are sealed.
    """

    kernel: RuntimeKernel
    artifacts: Any
    canonical: ProductionAndroidCanonicalReader
    step_store: SQLiteAndroidUiStepStore | None = None
    _seal: object = field(default_factory=object, repr=False)

    def resolve(
        self, query: ObservationGroundingQuery,
    ) -> TrustedObservationGroundingManifest | None:
        if not isinstance(query, ObservationGroundingQuery) or self.step_store is None:
            return None
        try:
            durable = self.step_store.grounding_facts(query)
            if durable is None:
                return None
            snapshot = self.canonical.inspect(query.task_id)
            if (
                snapshot.runner_kind != RUNNER_KIND
                or snapshot.runner_version != RUNNER_VERSION
                or snapshot.runner_binding_id is None
                or owner_scope_digest(snapshot.owner) != query.owner_scope_digest
                or opaque_digest("runner-binding", snapshot.runner_binding_id)
                != query.runner_binding_digest
                or snapshot.revision != query.revision
            ):
                return None

            matching = tuple(
                item for item in self.kernel.observations(query.task_id)
                if opaque_digest("observation-freshness", item.id)
                == query.freshness_digest
            )
            if len(matching) != 1:
                return None
            source = matching[0]
            screenshot = source.screenshot.artifact
            ui_artifact = source.ui_tree.artifact
            if (
                source.task_id != snapshot.task_id
                or source.device_id != snapshot.canonical_device_id
                or ui_artifact is None
                or source.screenshot.status.value != "AVAILABLE"
                or source.ui_tree.status.value != "AVAILABLE"
                or source.device_state.status.value != "AVAILABLE"
                or source.consistency.status.value != "consistent"
                or screenshot.content_type != "image/png"
                or ui_artifact.content_type not in {"application/xml", "text/xml"}
                or screenshot.sha256 != durable.get("screenshot_digest")
                or ui_artifact.sha256 != durable.get("ui_tree_digest")
                or _device_state_digest(source.device_state)
                != durable.get("device_state_digest")
                or snapshot.profile_generation != durable.get("profile_generation")
                or opaque_digest("profile-id", snapshot.profile_id)
                != durable.get("profile_id_digest")
                or opaque_digest("boot-id", snapshot.boot_id)
                != durable.get("boot_id_digest")
                or opaque_digest(
                    "canonical-device-id", snapshot.canonical_device_id,
                ) != durable.get("canonical_device_id_digest")
            ):
                return None

            observation = ObservationEnvelope(
                task_id=snapshot.task_id,
                profile_id=str(snapshot.profile_id),
                profile_generation=int(snapshot.profile_generation or 0),
                boot_id=str(snapshot.boot_id),
                canonical_device_id=str(snapshot.canonical_device_id),
                screenshot_ref=screenshot.sha256,
                screenshot_digest=screenshot.sha256,
                ui_tree_ref=ui_artifact.sha256,
                ui_tree_digest=ui_artifact.sha256,
                device_state_ref=str(durable["device_state_digest"]),
                device_state_digest=str(durable["device_state_digest"]),
                observed_at=source.captured_at,
                freshness_token=source.id,
                causality_command_id=(
                    str(durable["causality_command_id"])
                    if durable.get("causality_command_id") is not None else None
                ),
            )
            if observation_grounding_query(
                snapshot=snapshot,
                observation=observation,
                runner_kind=RUNNER_KIND,
                runner_version=RUNNER_VERSION,
                step_index=query.step_index,
            ) != query:
                return None

            # Both reads verify the immutable artifact length and SHA-256.  The
            # screenshot is not decoded here, but must exist and match before
            # the UI-tree facts can become grounding authority.
            self.artifacts.read(screenshot)
            nodes = tuple(
                GroundedUiNode(
                    node_id=item.node_id,
                    semantic_kind=item.semantic_kind,
                    semantic_marker=item.semantic_marker,
                    clickable=item.clickable,
                    bounds=item.bounds,
                )
                for item in _parse_ui_tree(
                    self.artifacts.read(ui_artifact),
                    source.device_state.screen_size,
                )
            )
            return TrustedObservationGroundingManifest(
                query=query,
                nodes=nodes,
                grounding_digest=grounding_manifest_digest(query, nodes),
                _attestation=self._seal,
            )
        except Exception:
            return None

    def validates(self, manifest: TrustedObservationGroundingManifest) -> bool:
        if (
            not isinstance(manifest, TrustedObservationGroundingManifest)
            or manifest._attestation is not self._seal
        ):
            return False
        resolved = self.resolve(manifest.query)
        return (
            resolved is not None
            and resolved.query == manifest.query
            and resolved.nodes == manifest.nodes
            and resolved.grounding_digest == manifest.grounding_digest
            and resolved._attestation is self._seal
        )


@dataclass(slots=True)
class ProductionModelArtifactLoader:
    kernel: RuntimeKernel
    artifacts: Any
    evidence: Any

    def load_for_model(self, observation: ObservationEnvelope) -> Mapping[str, Any]:
        source = self.kernel.load_observation(observation.freshness_token)
        screenshot = source.screenshot.artifact
        if (
            source.task_id != observation.task_id
            or screenshot.sha256 != observation.screenshot_digest
            or screenshot.sha256 != observation.screenshot_ref
        ):
            raise ProductionAndroidUiError("android_ui_model_artifact_crosswire")
        model_observation = self.evidence.record(
            observation.task_id,
            AndroidScreenshot(
                png_bytes=self.artifacts.read(screenshot),
                width=source.screenshot.width,
                height=source.screenshot.height,
            ),
        )
        return {
            "evidence_id": model_observation.evidence_id,
            "summary": model_observation.summary,
            "ui_nodes": tuple(
                {
                    "node_id": node.node_id,
                    "text": node.text,
                    "bounds": node.bounds,
                    "clickable": node.clickable,
                    "semantic_kind": node.semantic_kind,
                    "semantic_marker": node.semantic_marker,
                }
                for node in observation.ui_nodes
            ),
        }


@dataclass(slots=True)
class ProductionToolCaller:
    role_model: Any

    def call_tool(
        self,
        *,
        system: str,
        prompt: str,
        observations: tuple[Mapping[str, Any], ...],
        tool_name: str,
        description: str,
        parameters: Mapping[str, Any],
        max_tokens: int,
    ) -> Mapping[str, Any]:
        model_observations: list[ModelObservation] = []
        safe_context: list[object] = []
        for item in observations:
            evidence_id = item.get("evidence_id")
            summary = item.get("summary")
            if not isinstance(evidence_id, str) or not isinstance(summary, str):
                raise ProductionAndroidUiError("android_ui_model_artifact_invalid")
            model_observations.append(ModelObservation(evidence_id, summary))
            safe_context.append(item.get("ui_nodes", ()))
        bounded_prompt = prompt
        if safe_context:
            bounded_prompt += "\nTrusted bounded UI-tree context: " + repr(safe_context)[:12_000]
        result = self.role_model.call_tool(
            system=system,
            prompt=bounded_prompt,
            observations=tuple(model_observations),
            tool_name=tool_name,
            description=description,
            parameters=dict(parameters),
            max_tokens=max_tokens,
            # Criteria freezing is a bounded, schema-only compilation step.
            # Keep it on the local role model's low/no-thinking tool route so
            # llama.cpp emits the required structured call rather than an
            # unbounded reasoning response that cannot be accepted safely.
            reasoning_effort="low",
            reasoning_budget=0,
        )
        if not isinstance(result, Mapping):
            raise ProductionAndroidUiError("android_ui_model_result_invalid")
        return result


@dataclass(slots=True)
class ProductionCriteriaProvider:
    store: SQLiteAndroidUiStepStore
    caller: ProductionToolCaller

    def criteria_for(self, *, task_id: str, goal: str, revision: int) -> CriteriaRevision:
        safe_goal = sanitize_task_goal(goal)
        goal_digest = opaque_digest("android-ui-goal", safe_goal)
        existing = self.store.load_criteria(task_id, revision)
        if existing is not None:
            if (
                existing.goal_digest != goal_digest
                or not self.store.coverage_is_frozen(task_id, existing)
            ):
                raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
            return existing
        all_clauses = _goal_clauses(safe_goal)
        clauses = _observable_navigation_goal_clauses(safe_goal)
        # A redacted safety constraint (for example, a prohibited transport)
        # must remain in the canonical goal, but it is not a visible Android
        # target.  Only redaction inside an observable navigation clause makes
        # evidence freezing ambiguous.
        if not all_clauses or any(
            "[redacted" in clause.text.casefold() for clause in clauses
        ):
            raise ProductionAndroidUiError("android_ui_goal_coverage_unavailable")
        if not clauses:
            raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
        waypoint_clauses = _ordered_waypoint_clauses(safe_goal, clauses)
        terminal_clauses = clauses[len(waypoint_clauses):]
        if not terminal_clauses:
            raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
        response = self.caller.call_tool(
            system=(
                "Compile the complete user goal into observable Android UI conditions. "
                "Return exactly one terminal condition for each supplied terminal clause. "
                "When ordered waypoints are supplied, return exactly one ordered waypoint "
                "object for each of them before terminal success is allowed. "
                "Every semantic_value must be one exact visible page title, container label, "
                "or state label that preserves the semantic target and contains its supplied "
                "target core verbatim. Generic device wording such as 手机的 may be omitted. "
                "Do not substitute synonyms for a target core. Do not return alternatives, "
                "examples, or descendant list items."
            ),
            prompt=(
                f"Goal: {safe_goal}\nExact clauses: "
                + json.dumps([item.text for item in terminal_clauses], ensure_ascii=False)
                + "\nTerminal clauses: "
                + json.dumps([item.text for item in terminal_clauses], ensure_ascii=False)
                + "\nOrdered waypoint clauses: "
                + json.dumps([item.text for item in waypoint_clauses], ensure_ascii=False)
                + "\nTarget cores: "
                + json.dumps(
                    [_semantic_target_core(item.text) for item in clauses],
                    ensure_ascii=False,
                )
            ),
            observations=(),
            tool_name="freeze_android_ui_criteria",
            description="Freeze complete evidence-grounded criteria for one Task revision",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "criteria": {
                        "type": "array", "minItems": 1, "maxItems": _MODEL_CRITERIA_LIMIT,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "description": {"type": "string", "maxLength": 400},
                                "source_quote": {"type": "string", "maxLength": 800},
                                "marker_kind": {"type": "string", "enum": ["page_title", "container", "state"]},
                                "semantic_value": {"type": "string", "maxLength": 300},
                            },
                            "required": ["description", "source_quote", "marker_kind", "semantic_value"],
                        },
                    },
                    "waypoints": {
                        "type": "array", "minItems": len(waypoint_clauses),
                        "maxItems": len(waypoint_clauses),
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "source_quote": {"type": "string", "maxLength": 800},
                                "criteria": {
                                    "type": "array", "minItems": 1, "maxItems": _MODEL_CRITERIA_LIMIT,
                                    "items": {
                                        "type": "object", "additionalProperties": False,
                                        "properties": {
                                            "description": {"type": "string", "maxLength": 400},
                                            "marker_kind": {"type": "string", "enum": ["page_title", "container", "state"]},
                                            "semantic_value": {"type": "string", "maxLength": 300},
                                        },
                                        "required": ["description", "marker_kind", "semantic_value"],
                                    },
                                },
                            },
                            "required": ["source_quote", "criteria"],
                        },
                    },
                },
                "required": ["criteria"],
            },
            max_tokens=1_024,
        )
        values = response.get("criteria")
        if not isinstance(values, list) or not 1 <= len(values) <= _MODEL_CRITERIA_LIMIT:
            raise ProductionAndroidUiError("android_ui_criteria_invalid")
        criteria: list[Criterion] = []
        coverage: list[dict[str, Any]] = []
        covered_clauses: set[int] = set()
        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                raise ProductionAndroidUiError("android_ui_criteria_invalid")
            description = sanitize_task_goal(item.get("description"), maximum=400)
            source_quote = item.get("source_quote")
            kind = item.get("marker_kind")
            semantic_value = sanitize_summary(item.get("semantic_value"), maximum=300)
            matching = tuple(
                clause for clause in terminal_clauses
                if isinstance(source_quote, str)
                and source_quote.strip() == clause.text
            )
            if (
                kind not in {"page_title", "container", "state"}
                or not semantic_value
                or not matching
                or any(
                    not _semantic_value_proves_clause(semantic_value, clause.text)
                    or (
                        kind in {"page_title", "container"}
                        and _has_generic_device_locator(clause.text)
                        and _normalized_semantic_text(semantic_value)
                        != _semantic_target_core(clause.text)
                    )
                    for clause in matching
                )
            ):
                raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
            criterion_id = f"c{index + 1}"
            marker = evidence_marker(kind, semantic_value)
            criteria.append(Criterion(criterion_id, description, (marker,)))
            for clause in matching:
                covered_clauses.add(clause.index)
                coverage.append({
                    "criterion_id": criterion_id,
                    "clause_index": clause.index,
                    "source_start": clause.start,
                    "source_end": clause.end,
                    "source_digest": opaque_digest(
                        "android-ui-goal-clause", clause.text,
                    ),
                    "semantic_marker": marker,
                })
        raw_waypoints = response.get("waypoints")
        if not waypoint_clauses:
            if raw_waypoints not in (None, []):
                raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
            frozen_waypoints: tuple[OrderedWaypoint, ...] = ()
        elif not isinstance(raw_waypoints, list) or len(raw_waypoints) != len(waypoint_clauses):
            raise ProductionAndroidUiError("android_ui_criteria_coverage_incomplete")
        else:
            compiled_waypoints: list[OrderedWaypoint] = []
            for ordinal, (source, raw_waypoint) in enumerate(
                zip(waypoint_clauses, raw_waypoints), start=1,
            ):
                if (
                    not isinstance(raw_waypoint, Mapping)
                    or raw_waypoint.get("source_quote") != source.text
                    or not isinstance(raw_waypoint.get("criteria"), list)
                    or not raw_waypoint["criteria"]
                ):
                    raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
                waypoint_criteria: list[Criterion] = []
                for criterion_index, raw_criterion in enumerate(raw_waypoint["criteria"], start=1):
                    if not isinstance(raw_criterion, Mapping):
                        raise ProductionAndroidUiError("android_ui_criteria_invalid")
                    description = sanitize_task_goal(raw_criterion.get("description"), maximum=400)
                    kind = raw_criterion.get("marker_kind")
                    semantic_value = sanitize_summary(raw_criterion.get("semantic_value"), maximum=300)
                    if (
                        kind not in {"page_title", "container", "state"}
                        or not semantic_value
                        or not _semantic_value_proves_clause(semantic_value, source.text)
                        or (
                            kind in {"page_title", "container"}
                            and _has_generic_device_locator(source.text)
                            and _normalized_semantic_text(semantic_value)
                            != _semantic_target_core(source.text)
                        )
                    ):
                        raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
                    marker = evidence_marker(kind, semantic_value)
                    criterion_id = f"w{ordinal}c{criterion_index}"
                    waypoint_criteria.append(Criterion(criterion_id, description, (marker,)))
                    coverage.append({
                        "criterion_id": criterion_id,
                        "clause_index": source.index,
                        "source_start": source.start,
                        "source_end": source.end,
                        "source_digest": opaque_digest("android-ui-goal-clause", source.text),
                        "semantic_marker": marker,
                    })
                compiled_waypoints.append(OrderedWaypoint(
                    f"waypoint-{ordinal}", ordinal,
                    opaque_digest("android-ui-goal-clause", source.text),
                    tuple(waypoint_criteria),
                    criteria_digest(tuple(waypoint_criteria)),
                ))
                covered_clauses.add(source.index)
            frozen_waypoints = tuple(compiled_waypoints)
        if covered_clauses != {item.index for item in clauses}:
            raise ProductionAndroidUiError("android_ui_criteria_coverage_incomplete")
        coverage_value = tuple(coverage)
        coverage_digest = criteria_coverage_digest(coverage_value)
        criteria_value = tuple(criteria)
        frozen = CriteriaRevision(
            revision,
            criteria_value,
            criteria_digest(
                criteria_value,
                goal_digest=goal_digest,
                coverage_digest=coverage_digest,
                waypoints=frozen_waypoints,
            ),
            goal_digest,
            coverage_digest,
            frozen_waypoints,
        )
        return self.store.freeze_criteria(
            task_id, frozen, coverage=coverage_value,
        )

    def coverage_matches_goal(
        self, *, task_id: str, goal: str, criteria: CriteriaRevision,
    ) -> bool:
        safe_goal = sanitize_task_goal(goal)
        return (
            criteria.goal_digest == opaque_digest("android-ui-goal", safe_goal)
            and self.store.coverage_is_frozen(task_id, criteria)
        )


class TrustedTreeSemanticRole:
    """Ground terminal verdicts only in typed current UI-tree evidence.

    The v1 parser has no trusted checked/selected/value projection.  Its
    text-only ``state`` label can therefore describe a switch row while the
    opposite boolean value is active, so state markers remain unknown until a
    versioned adapter supplies that typed evidence.
    """

    def verify_goal(
        self,
        context: PlanningContext,
        *,
        before: ObservationEnvelope | None,
        after: ObservationEnvelope,
        latest_step_index: int,
        already_satisfied: bool,
    ) -> tuple[CriterionVerdict, ...]:
        del before, latest_step_index, already_satisfied
        verdicts: list[CriterionVerdict] = []
        for criterion in context.criteria.criteria:
            anchors: list[Anchor] = []
            for marker in criterion.required_evidence_markers:
                marker_kind = marker.split(":", 1)[0]
                node = next((
                    item for item in after.ui_nodes
                    if item.semantic_marker == marker
                    and marker_kind in {"page_title", "container"}
                    and item.semantic_kind == marker_kind
                    and not (item.clickable and item.semantic_kind == "navigation")
                ), None)
                if node is None:
                    anchors = []
                    break
                anchors.append(Anchor(
                    "ui_node", after.freshness_token, node_id=node.node_id,
                    text=node.text, bounds=node.bounds, semantic_marker=node.semantic_marker,
                ))
            verdicts.append(CriterionVerdict(
                criterion.criterion_id,
                "satisfied" if anchors and len(anchors) == len(criterion.required_evidence_markers) else "unknown",
                tuple(anchors),
            ))
        return tuple(verdicts)


class DigestActionVerifier:
    def verify(self, context: ActionVerificationContext) -> PrimitiveVerification:
        changed = (
            context.observation_before.screenshot_digest != context.observation_after.screenshot_digest
            or context.observation_before.ui_tree_digest != context.observation_after.ui_tree_digest
            or context.observation_before.device_state_digest != context.observation_after.device_state_digest
        )
        return PrimitiveVerification(
            progress=changed,
            uncertain=False,
            reason="fresh_observation_changed" if changed else "fresh_observation_unchanged",
        )


@dataclass(slots=True)
class ProductionAndroidVerificationAttestor:
    """Revalidate one immutable K2 row before K3 can trust it.

    The attestor has no mutation surface.  It derives the requested K3 scope
    again from current canonical ownership/Profile state and the latest exact
    RuntimeKernel observation, then verifies the K2 record and its
    domain-separated record id.  Any unavailable or drifted fact returns cold.
    """

    step_store: SQLiteAndroidUiStepStore
    canonical: ProductionAndroidCanonicalReader
    profiles: Any
    kernel: RuntimeKernel
    runtime_metadata: Any | None = None

    def attest(
        self, query: TrustedVerificationQuery,
    ) -> TrustedVerificationAttestation | None:
        stage = "input"

        def cold() -> None:
            logging.getLogger(__name__).warning(
                "K3 K2-attestation unavailable at %s.", stage,
            )

        try:
            if not isinstance(query, TrustedVerificationQuery):
                return None
            if (
                query.task_id != query.scope.task_id
                or query.scope_digest != query.scope.attestation_digest
                or query.revision != query.scope.criteria_revision
                or query.step_index < 0
            ):
                cold()
                return None
            stage = "canonical"
            snapshot = self.canonical.inspect(query.task_id)
            stage = "durable_read"
            criteria = self.step_store.load_criteria(
                query.task_id, query.revision,
            )
            record = self.step_store.immutable_goal_verification(
                task_id=query.task_id,
                revision=query.revision,
                step_index=query.step_index,
            )
            source = self.kernel.latest_observation(query.task_id)
            if criteria is None or record is None or source is None:
                cold()
                return None
            stage = "scope"
            expected_scope = _authenticated_scope_for_observation(
                snapshot=snapshot,
                criteria=criteria,
                source=source,
                profiles=self.profiles,
                runtime_metadata=self.runtime_metadata,
            )
            if expected_scope != query.scope:
                cold()
                return None
            stage = "record"
            if not _immutable_record_matches(
                record=record,
                snapshot=snapshot,
                criteria=criteria,
                source=source,
                step_index=query.step_index,
            ):
                cold()
                return None
            stage = "record_id"
            record_id = _verification_record_id(expected_scope, record)
            if record_id != query.record_id:
                cold()
                return None
            return TrustedVerificationAttestation(
                record_id=record_id,
                task_id=query.task_id,
                scope_digest=query.scope_digest,
                revision=query.revision,
                step_index=query.step_index,
                # K2 step indexes are zero based; K3 checkpoints are positive.
                checkpoint_index=query.step_index + 1,
                already_satisfied=bool(record.get("already_satisfied")),
                immutable_record=record,
            )
        except Exception:
            cold()
            return None


@dataclass(slots=True)
class ProductionAndroidExperiencePort:
    """Exact-scoped, advisory K3 sidecar for the general Android UI runner."""

    service: ExperienceService
    profiles: Any
    kernel: RuntimeKernel
    runtime_metadata: Any | None = None

    def retrieve(
        self, *, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        observation: ObservationEnvelope, step_id: str, checkpoint_index: int,
    ) -> tuple[ExperienceHint, ...]:
        del checkpoint_index
        try:
            scope = self._scope(snapshot, criteria, observation)
            if self.service.begin_authenticated_android_episode(scope=scope) is None:
                return ()
            values = self.service.retrieve_planner_hints(
                scope=scope,
                checkpoint=CheckpointBaselineQuery(
                    snapshot=snapshot,
                    criteria=criteria,
                    observation=observation,
                    runner_kind=RUNNER_KIND,
                    runner_version=RUNNER_VERSION,
                ),
                step_id=step_id,
            )
            return tuple(
                ExperienceHint(
                    candidate_id=item.candidate_id,
                    retrieval_id=item.retrieval_id,
                    kind=item.kind,
                    action_kind=item.action_kind,
                    semantic_anchor=item.semantic_anchor,
                    expected_scene_marker=item.expected_scene_marker,
                    confidence=item.confidence,
                    support_count=item.support_count,
                    failure_count=item.failure_count,
                    provenance_count=item.provenance_count,
                )
                for item in values[:8]
            )
        except Exception:
            return ()

    def record_step(
        self, *, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        step_id: str, checkpoint_index: int, action: Any,
        before: ObservationEnvelope, after: ObservationEnvelope,
        verification: Any, primitive_outcome: str,
        hints: tuple[ExperienceHint, ...], selected_hint: ExperienceHint | None = None,
        selected_candidate_id: str | None = None,
        selected_retrieval_id: str | None = None,
    ) -> None:
        del before
        del checkpoint_index
        try:
            if primitive_outcome == "uncertain" or action.kind == "wait":
                return
            scope = self._scope(
                snapshot, criteria, after, require_latest=False,
            )
            if self.service.begin_authenticated_android_episode(scope=scope) is None:
                return
            checkpoint = CheckpointBaselineQuery(
                snapshot=snapshot,
                criteria=criteria,
                observation=after,
                runner_kind=RUNNER_KIND,
                runner_version=RUNNER_VERSION,
            )
            candidate = self.service.record_android_ui_progress(
                scope=scope,
                step_id=step_id,
                checkpoint=checkpoint,
                action_kind=action.kind,
                # Criterion text and UI text are intentionally excluded.  The
                # opaque goal digest plus marker kinds is stable and useful
                # enough for exact-scoped task-local planning.
                semantic_anchor=_experience_semantic_anchor(criteria),
                kind=("progress" if primitive_outcome == "progress" else "negative"),
            )
            # Retrieval alone is not usage.  Only the actor's durable explicit
            # selected_hint_id is attributed, and an uncertain/independent K2
            # result stays ``unknown`` rather than inventing support.
            candidate_id = (
                selected_hint.candidate_id if selected_hint is not None
                else selected_candidate_id
            )
            retrieval_id = (
                selected_hint.retrieval_id if selected_hint is not None
                else selected_retrieval_id
            )
            if isinstance(candidate_id, str) and isinstance(retrieval_id, str):
                self.service.record_android_hint_outcome(
                    scope=scope,
                    retrieval_id=retrieval_id,
                    candidate_id=candidate_id,
                    step_id=step_id,
                    result="unknown",
                    checkpoint=checkpoint,
                    verification=None,
                )
            if candidate is None or verification.already_satisfied:
                return
            immutable = verification.private_durable_record()
            record_id = _verification_record_id(scope, immutable)
            # K3 itself rejects unknown app/build versions and any untrusted,
            # stale, already-satisfied, or non-terminal semantic record.  Scan
            # only this exact task scope so a final success can promote prior
            # verified progress edges, never neighbouring Task evidence.
            candidates = self.service.store.android_candidates_for_scope(scope)
            for source, reusable in candidates:
                if reusable or source.status != "task_local" or source.kind == "negative":
                    continue
                self.service.derive_verified_android_candidate(
                    scope=scope,
                    source_candidate_id=source.candidate_id,
                    verification=verification,
                    checkpoint=checkpoint,
                    verification_record_id=record_id,
                )
        except Exception:
            # A partially unavailable Experience DB, a scope drift, or an
            # attestation miss can never invalidate the already durable K2
            # step or change the canonical Task outcome.
            return

    def reconcile_verified_terminal(
        self, *, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        after: ObservationEnvelope, durable: Mapping[str, Any],
        ui_tree_by_checkpoint: Mapping[int, str] = {},
    ) -> int:
        """Promote only existing K2-backed local candidates without a device step."""

        try:
            if durable.get("overall") != "satisfied" or durable.get("already_satisfied"):
                return 0
            scope = self._scope(
                snapshot, criteria, after, require_latest=False,
            )
            legacy = {
                candidate.candidate_id: ui_tree_by_checkpoint[candidate.source_checkpoint_index]
                for candidate, reusable in self.service.store.android_candidates_for_scope(scope)
                if reusable and candidate.source_checkpoint_index in ui_tree_by_checkpoint
            }
            self.derive_structural_candidates(scope=scope, ui_tree_digests=legacy)
            checkpoint = CheckpointBaselineQuery(
                snapshot=snapshot,
                criteria=criteria,
                observation=after,
                runner_kind=RUNNER_KIND,
                runner_version=RUNNER_VERSION,
            )
            record_id = _verification_record_id(
                scope, durable,
            )
            attestation = self.service.trusted_android_terminal_attestation(
                scope=scope,
                record_id=record_id,
                revision=int(durable["revision"]),
                step_index=int(durable["latest_step_index"]),
            )
            if attestation is None:
                logging.getLogger(__name__).warning(
                    "K3 terminal promotion skipped at k2_attestation.",
                )
                return 0
            promoted = 0
            eligible = 0
            for source, reusable in self.service.store.android_candidates_for_scope(scope):
                if reusable or source.status != "task_local" or source.kind == "negative":
                    continue
                eligible += 1
                if self.service.derive_verified_android_candidate_from_attestation(
                    scope=scope,
                    source_candidate_id=source.candidate_id,
                    checkpoint=checkpoint,
                    attestation=attestation,
                ) is not None:
                    promoted += 1
            logging.getLogger(__name__).warning(
                "K3 terminal promotion evaluated %d local candidates and promoted %d.",
                eligible,
                promoted,
            )
            return promoted
        except Exception as error:
            logging.getLogger(__name__).warning(
                "K3 terminal promotion skipped: %s", type(error).__name__,
            )
            return 0

    def derive_structural_candidates(
        self, *, scope: AuthenticatedAndroidScope, ui_tree_digests: Mapping[str, str],
    ) -> int:
        derived = 0
        for candidate, reusable in self.service.store.android_candidates_for_scope(scope):
            if not reusable:
                continue
            digest = ui_tree_digests.get(candidate.candidate_id)
            if digest and self.service.derive_structural_android_candidate(
                source=candidate, ui_tree_digest=digest,
            ) is not None:
                derived += 1
        return derived

    def _scope(
        self, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        observation: ObservationEnvelope, *, require_latest: bool = True,
    ) -> AuthenticatedAndroidScope:
        source = self.kernel.load_observation(observation.freshness_token)
        latest = self.kernel.latest_observation(snapshot.task_id)
        if (
            observation.task_id != snapshot.task_id
            or observation.profile_id != snapshot.profile_id
            or observation.profile_generation != snapshot.profile_generation
            or observation.boot_id != snapshot.boot_id
            or observation.canonical_device_id != snapshot.canonical_device_id
            or source.task_id != observation.task_id
            or source.device_id != observation.canonical_device_id
            or (require_latest and (latest is None or latest.id != source.id))
            or source.screenshot.artifact.sha256 != observation.screenshot_digest
            or (
                source.ui_tree.artifact.sha256
                if source.ui_tree.artifact is not None else None
            ) != observation.ui_tree_digest
            or _device_state_digest(source.device_state) != observation.device_state_digest
        ):
            raise ProductionAndroidUiError("android_ui_experience_observation_crosswire")
        return _authenticated_scope_for_observation(
            snapshot=snapshot,
            criteria=criteria,
            source=source,
            profiles=self.profiles,
            runtime_metadata=self.runtime_metadata,
        )


@dataclass(slots=True)
class ProductionKernelPhysicalDispatcher(GenericPhysicalDispatcher):
    kernel: RuntimeKernel
    causality: _CausalityLedger
    claims: SQLiteGenericCommandStore
    canonical_store: Any

    def dispatch(
        self,
        *,
        canonical_device_id: str,
        command_type: str,
        payload: Mapping[str, object],
        command_id: str,
        binding: GenericProfileBinding,
        claim: GenericCommandClaim,
    ) -> GenericDispatchResult:
        authoritative_claim = self.claims.get(command_id)
        if (
            authoritative_claim is None
            or authoritative_claim != claim
            or authoritative_claim.state != "DISPATCHING"
            or authoritative_claim.command_id != command_id
            or authoritative_claim.canonical_device_id != canonical_device_id
            or authoritative_claim.owner_principal_id != binding.owner_principal_id
            or authoritative_claim.controller_id != binding.controller_id
            or authoritative_claim.profile_id != binding.profile_id
            or authoritative_claim.profile_generation != binding.profile_generation
            or authoritative_claim.device_boot_id != binding.device_boot_id
        ):
            raise ProductionAndroidUiError("android_ui_physical_claim_missing")
        if canonical_device_id != binding.canonical_device_id:
            raise ProductionAndroidUiError("android_ui_physical_binding_crosswire")
        canonical_service = CanonicalTaskService(
            self.canonical_store,
            principal_id=claim.owner_principal_id,
            controller_id=claim.controller_id,
        )
        try:
            committed = canonical_service.require_active_runner_dispatch(
                claim.task_id,
                dispatch_id=claim.dispatch_id,
                command_id=claim.command_id,
            )
        except (SessionNotFound, TaskDispatchConflict, KeyError) as error:
            raise ProductionAndroidUiError(
                "android_ui_physical_canonical_effect_missing"
            ) from error
        request = committed.request
        if (
            committed.dispatch_id != claim.dispatch_id
            or committed.task_id != claim.task_id
            or committed.owner_principal_id != claim.owner_principal_id
            or committed.controller_id != claim.controller_id
            or request.step_id != claim.step_id
            or request.subtask_id != claim.subtask_id
            or request.action_id != claim.action_id
            or request.action_id != command_id
            or request.command_id != claim.command_id
            or request.command_id != command_id
            or request.runner_kind != RUNNER_KIND
            or request.runner_version != str(RUNNER_VERSION)
            or request.profile_id != claim.profile_id
            or request.profile_generation != claim.profile_generation
            or request.device_boot_id != claim.device_boot_id
            or request.canonical_device_id != claim.canonical_device_id
            or request.canonical_device_id != canonical_device_id
            or request.command_type != claim.command_type
            or request.command_type != command_type
            or request.payload_digest != claim.payload_digest
            or command_payload_digest(command_type, payload) != request.payload_digest
            or binding.owner_principal_id != committed.owner_principal_id
            or binding.controller_id != committed.controller_id
            or binding.profile_id != request.profile_id
            or binding.profile_generation != request.profile_generation
            or binding.device_boot_id != request.device_boot_id
            or binding.canonical_device_id != request.canonical_device_id
        ):
            raise ProductionAndroidUiError(
                "android_ui_physical_canonical_effect_mismatch"
            )
        binding_task_id = _task_for_action(self.kernel, command_id)
        if binding_task_id != claim.task_id:
            raise ProductionAndroidUiError("android_ui_physical_action_crosswire")
        action = self.kernel.load_action(binding_task_id, command_id)
        if (
            action.type.value != command_type
            or action.task_id != binding_task_id
            or action.id != command_id
        ):
            raise ProductionAndroidUiError("android_ui_physical_action_crosswire")
        _validate_kernel_payload(action.params, payload, command_type)
        execution = self.kernel.execute_action(task_id=action.task_id, action_id=command_id)
        # A rejected transport result proves that no command-caused after
        # observation exists.  Recording causality here would let the next
        # unrelated capture masquerade as evidence for a command the
        # transport explicitly did not accept.
        if execution.accepted:
            self.causality.record(action.task_id, command_id, execution.finished_at)
        return GenericDispatchResult(execution.accepted, "accepted" if execution.accepted else "rejected")


@dataclass(slots=True)
class ProductionGenericEvidenceLookup(GenericEvidenceLookup):
    """Reload exact action, receipt and after-observation facts from Kernel storage."""

    kernel: RuntimeKernel

    def resolve_reconciliation(
        self, *, command_id: str, observation_id: str, claim: GenericCommandClaim,
    ) -> GenericReconciliationEvidence:
        try:
            action = self.kernel.load_action(claim.task_id, claim.action_id)
            execution = self.kernel.load_action_execution(claim.action_id)
            observation = self.kernel.load_observation(observation_id)
        except RecordNotFound as error:
            raise GenericCommandConflict(
                "authoritative command receipt or after observation is unavailable"
            ) from error
        if (
            command_id != claim.command_id
            or action.id != claim.action_id
            or action.task_id != claim.task_id
            or execution.action_id != claim.action_id
            or not execution.accepted
            or action.status is not ActionStatus.EXECUTED
            or observation.task_id != claim.task_id
            or observation.device_id != claim.canonical_device_id
        ):
            raise GenericCommandConflict("authoritative reconciliation binding changed")
        receipt_boundary = claim.settled_at or execution.finished_at
        if (
            _as_timestamp(observation.capture_started_at) <= _as_timestamp(receipt_boundary)
            or _as_timestamp(observation.captured_at) <= _as_timestamp(receipt_boundary)
        ):
            raise GenericCommandConflict("authoritative after observation predates command receipt")
        refs = [str(observation.screenshot.artifact.reference)]
        if observation.ui_tree.artifact is not None:
            refs.append(str(observation.ui_tree.artifact.reference))
        return GenericReconciliationEvidence(
            task_id=claim.task_id,
            dispatch_id=claim.dispatch_id,
            step_id=claim.step_id,
            subtask_id=claim.subtask_id,
            action_id=claim.action_id,
            owner_principal_id=claim.owner_principal_id,
            controller_id=claim.controller_id,
            profile_id=claim.profile_id,
            profile_generation=claim.profile_generation,
            device_boot_id=claim.device_boot_id,
            canonical_device_id=claim.canonical_device_id,
            observation_id=observation.id,
            evidence_refs=tuple(refs),
            command_id=claim.command_id,
            captured_at=observation.captured_at,
            # RuntimeKernel returns only after its SQLite observation write
            # transaction has committed.  Its durable observation record uses
            # the completed capture timestamp as the persistence boundary.
            persisted_at=observation.captured_at,
            observed_effect=True,
        )


@dataclass(slots=True)
class ProductionAndroidDispatchPort:
    canonical: ProductionAndroidCanonicalReader
    kernel: RuntimeKernel
    profiles: Any
    claims: SQLiteGenericCommandStore
    physical: ProductionKernelPhysicalDispatcher
    evidence_lookup: ProductionGenericEvidenceLookup

    def dispatch(self, snapshot: CanonicalSnapshot, intent: AndroidUiActionIntent) -> DispatchReceipt:
        if self.canonical.inspect(snapshot.task_id) != snapshot:
            raise GenericCommandConflict("canonical Android UI snapshot changed before dispatch")
        observation = self.kernel.latest_observation(snapshot.task_id)
        if observation is None or observation.id == "" or observation.id != self.kernel.load_task(snapshot.task_id).last_observation_id:
            raise GenericCommandConflict("fresh RuntimeKernel observation is unavailable")
        payload = _kernel_payload(intent, observation.device_state.screen_size)
        command_id = _command_id(snapshot, intent)
        action = self._ensure_action(snapshot, intent, command_id, observation.id, payload)
        if action.id != command_id:
            raise GenericCommandConflict("RuntimeKernel action identity changed")
        service = CanonicalTaskService(
            self.canonical.runtime_store,
            principal_id=snapshot.owner.principal_id,
            controller_id=snapshot.owner.controller_id,
        )
        request = RunnerDispatchRequest(
            step_id=_step_id(snapshot.task_id, intent),
            action_id=command_id,
            command_id=command_id,
            runner_kind=RUNNER_KIND,
            runner_version=str(RUNNER_VERSION),
            subtask_id=str(snapshot.runner_binding_id),
            profile_id=str(snapshot.profile_id),
            profile_generation=int(snapshot.profile_generation or 0),
            device_boot_id=str(snapshot.boot_id),
            canonical_device_id=str(snapshot.canonical_device_id),
            command_type=intent.action.kind,
            payload_digest=command_payload_digest(intent.action.kind, payload),
            expected_revision=snapshot.revision,
        )
        attempt = GenericKernelCommandPort(
            tasks=service,
            claims=self.claims,
            dispatcher=self.physical,
            profiles=self.profiles,
            evidence_lookup=self.evidence_lookup,
        ).dispatch_once(task_id=snapshot.task_id, request=request, payload=payload)
        # K1 already settles zero-side-effect binding failures as
        # ``preflight_rejected``.  A physical dispatcher that was invoked but
        # explicitly rejected the command crossed a different boundary: its
        # canonical effect is settled as ``not_observed`` and it must never
        # acquire command causality for a later screen capture.
        if (
            attempt.physically_dispatched
            and attempt.claim.state == "SETTLED"
            and attempt.claim.outcome == "rejected"
        ):
            service.settle_runner_effect(
                snapshot.task_id,
                command_id=command_id,
                outcome="not_observed",
            )
        return DispatchReceipt(command_id, intent.action_intent_id)

    def reconcile(self, snapshot: CanonicalSnapshot, intent: AndroidUiActionIntent) -> DispatchReceipt:
        command_id = _command_id(snapshot, intent)
        claim = self.claims.get(command_id)
        if (
            claim is None
            or claim.task_id != snapshot.task_id
            or claim.action_id != command_id
            or claim.profile_id != snapshot.profile_id
            or claim.profile_generation != snapshot.profile_generation
            or claim.device_boot_id != snapshot.boot_id
            or claim.canonical_device_id != snapshot.canonical_device_id
        ):
            raise GenericCommandConflict("generic Android UI command cannot be reconciled")
        return DispatchReceipt(command_id, intent.action_intent_id)

    def settle_after(
        self,
        *,
        snapshot: CanonicalSnapshot,
        intent: AndroidUiActionIntent,
        receipt: DispatchReceipt,
        before: ObservationEnvelope,
        after: ObservationEnvelope,
        primitive: PrimitiveVerification,
    ) -> None:
        command_id = _command_id(snapshot, intent)
        if receipt.command_id != command_id or after.causality_command_id != command_id:
            raise GenericCommandConflict("after observation is not caused by this command")
        claim = self.claims.get(command_id)
        if claim is None or claim.state not in {"SETTLED", "OUTCOME_UNKNOWN"}:
            raise GenericCommandConflict("generic command has no durable transport boundary")
        if claim.state == "SETTLED" and claim.outcome != "accepted":
            raise GenericCommandConflict(
                "fresh after evidence requires an accepted transport outcome"
            )
        service = CanonicalTaskService(
            self.canonical.runtime_store,
            principal_id=snapshot.owner.principal_id,
            controller_id=snapshot.owner.controller_id,
        )
        GenericKernelCommandPort(
            tasks=service,
            claims=self.claims,
            dispatcher=self.physical,
            profiles=self.profiles,
            evidence_lookup=self.evidence_lookup,
        ).reconcile(
            command_id=command_id,
            observation_id=after.freshness_token,
        )
        action = self.kernel.load_action(snapshot.task_id, command_id)
        if action.status is not ActionStatus.EXECUTED:
            return
        try:
            self.kernel.load_verification(command_id)
            return
        except RecordNotFound:
            pass
        after_source = self.kernel.load_observation(after.freshness_token)
        refs = [str(after_source.screenshot.artifact.reference)]
        if after_source.ui_tree.artifact is not None:
            refs.append(str(after_source.ui_tree.artifact.reference))
        self.kernel.verify_action(
            task_id=snapshot.task_id,
            action_id=command_id,
            before_observation_id=before.freshness_token,
            after_observation_id=after.freshness_token,
            verdict=(VerificationVerdict.SUCCESS if primitive.progress else VerificationVerdict.FAIL),
            reason=primitive.reason,
            evidence_refs=tuple(refs),
            method=VerificationMethod.RUNTIME_RULE,
            verification_call_id=f"{RUNNER_KIND}-primitive-v{RUNNER_VERSION}",
            complete_stage=False,
        )

    def _ensure_action(
        self,
        snapshot: CanonicalSnapshot,
        intent: AndroidUiActionIntent,
        command_id: str,
        observation_id: str,
        payload: Mapping[str, object],
    ) -> Any:
        try:
            action = self.kernel.load_action(snapshot.task_id, command_id)
        except RecordNotFound:
            action = self.kernel.propose_action(
                task_id=snapshot.task_id,
                stage_id=_stage_id(snapshot.task_id),
                based_on_observation_id=observation_id,
                action_type=ActionType(intent.action.kind),
                params=payload,
                expected_outcome="one bounded primitive produces fresh after evidence",
                proposed_by_call_id=intent.action_intent_id,
                action_id=command_id,
            )
        if (
            action.task_id != snapshot.task_id
            or action.stage_id != _stage_id(snapshot.task_id)
            or action.based_on_observation_id != observation_id
            or action.type.value != intent.action.kind
            or action.proposed_by_call_id != intent.action_intent_id
        ):
            raise GenericCommandConflict("RuntimeKernel action binding changed")
        return action


@dataclass(slots=True)
class ProductionAndroidUiRunner:
    handler: AndroidUiAgentV1Handler
    criteria: ProductionCriteriaProvider
    observations: ProductionAndroidObservationProvider

    def criteria_for(self, *, task_id: str, goal: str, revision: int) -> CriteriaRevision:
        return self.criteria.criteria_for(
            task_id=task_id, goal=goal, revision=revision,
        )

    def one_step(self, *, task_id: str, goal: str, criteria: CriteriaRevision):
        if not self.criteria.coverage_matches_goal(
            task_id=task_id, goal=goal, criteria=criteria,
        ):
            raise ProductionAndroidUiError("android_ui_criteria_coverage_invalid")
        self.observations.prepare(task_id=task_id, goal=goal, criteria=criteria)
        return self.handler.one_step(task_id=task_id, goal=goal, criteria=criteria)

    def trusted_terminal(self, *, task_id: str, criteria: CriteriaRevision):
        if not self.criteria.store.coverage_is_frozen(task_id, criteria):
            return None
        return self.handler.trusted_terminal(task_id=task_id, criteria=criteria)

    def reconcile_terminal_experience(self, *, task_id: str) -> int:
        """Replay K3 promotion from one immutable, already-succeeded K2 record."""

        def skipped(reason: str) -> int:
            logging.getLogger(__name__).warning(
                "K3 terminal reconciliation skipped at %s.", reason,
            )
            return 0

        try:
            experience = self.handler.experience
            reconcile = getattr(experience, "reconcile_verified_terminal", None)
            rehydrate = getattr(self.observations, "rehydrate", None)
            if not callable(reconcile) or not callable(rehydrate):
                return skipped("experience_port")
            snapshot = self.handler.canonical.inspect(task_id)
            criteria = self.criteria.store.load_criteria(task_id, snapshot.revision)
            if criteria is None:
                return skipped("criteria")
            satisfied_steps = [
                item["latest_step_index"]
                for item in self.criteria.store.safe_goal_verifications(task_id)
                if item.get("overall") == "satisfied"
            ]
            if not satisfied_steps:
                return skipped("satisfied_record")
            durable = self.criteria.store.immutable_goal_verification(
                task_id=task_id,
                revision=snapshot.revision,
                step_index=max(satisfied_steps),
            )
            if durable is None or durable.get("overall") != "satisfied":
                return skipped("immutable_record")
            before = rehydrate(snapshot, durable.get("before", {}))
            after = rehydrate(snapshot, durable.get("after", {}))
            if before is None or after is None:
                return skipped("observation_rehydration")
            ui_tree_by_checkpoint = {
                int(item["latest_step_index"]) + 1: str(item["after"]["ui_tree_digest"])
                for item in self.criteria.store.structural_goal_verifications(task_id)
                if isinstance(item.get("after"), Mapping)
                and isinstance(item["after"].get("ui_tree_digest"), str)
                and _SHA256.fullmatch(str(item["after"]["ui_tree_digest"]))
            }
            return int(reconcile(
                snapshot=snapshot,
                criteria=criteria,
                after=after,
                durable=durable,
                ui_tree_by_checkpoint=ui_tree_by_checkpoint,
            ))
        except Exception as error:
            # A K3 replay cannot affect a terminal K2 task.  Keep the log
            # diagnostic deliberately non-sensitive: no goal, UI text,
            # observation payload, or model material is emitted.
            logging.getLogger(__name__).warning(
                "K3 terminal reconciliation skipped: %s", type(error).__name__,
            )
            return 0


@dataclass(slots=True)
class ProductionAndroidUiComposition:
    runner: ProductionAndroidUiRunner
    step_store: SQLiteAndroidUiStepStore
    command_store: SQLiteGenericCommandStore
    grounding: ProductionRuntimeObservationGrounding
    experience: ProductionAndroidExperiencePort | None = None
    verification_attestor: ProductionAndroidVerificationAttestor | None = None


def compose_production_android_ui_runner(
    *,
    data_dir: Path,
    kernel: RuntimeKernel,
    artifacts: Any,
    runtime_store: Any,
    profiles: Any,
    role_model: Any,
    evidence: Any,
    experience_service: ExperienceService | None = None,
    runtime_metadata: Any | None = None,
    managed_file: Callable[[str], Path] | None = None,
) -> ProductionAndroidUiComposition:
    """Build the general runner without touching a model, profile, or device."""

    canonical = ProductionAndroidCanonicalReader(runtime_store, profiles)
    grounding = ProductionRuntimeObservationGrounding(
        kernel=kernel,
        artifacts=artifacts,
        canonical=canonical,
    )
    runtime_file = managed_file or (lambda relative: data_dir / relative)
    step_store = SQLiteAndroidUiStepStore(
        runtime_file("runtime/android-ui-steps.db"),
        grounding=grounding,
    )
    grounding.step_store = step_store
    command_store = SQLiteGenericCommandStore(runtime_file("runtime/android-ui-command-claims.db"))
    command_store.initialize()
    causality = _CausalityLedger()
    observations = ProductionAndroidObservationProvider(
        kernel, artifacts, causality, command_store,
    )
    caller = ProductionToolCaller(role_model)
    artifact_loader = ProductionModelArtifactLoader(kernel, artifacts, evidence)
    roles = BoundedToolRoleAdapter(caller, artifact_loader)
    physical = ProductionKernelPhysicalDispatcher(
        kernel, causality, command_store, runtime_store,
    )
    evidence_lookup = ProductionGenericEvidenceLookup(kernel)
    dispatch = ProductionAndroidDispatchPort(
        canonical, kernel, profiles, command_store, physical, evidence_lookup,
    )
    verification_attestor: ProductionAndroidVerificationAttestor | None = None
    experience: ProductionAndroidExperiencePort | None = None
    if experience_service is not None:
        verification_attestor = ProductionAndroidVerificationAttestor(
            step_store=step_store,
            canonical=canonical,
            profiles=profiles,
            kernel=kernel,
            runtime_metadata=runtime_metadata,
        )
        # Share the canonical Experience DB while installing only this K2
        # attestation capability on the general-runner facade.  Other legacy
        # call sites keep their existing service semantics unchanged.
        scoped_service = ExperienceService(
            experience_service.store,
            enabled=experience_service.enabled,
            observation_payload=experience_service.observation_payload,
            trusted_verification_port=verification_attestor,
            checkpoint_baseline_port=step_store.checkpoint_attestation_port(),
        )
        experience = ProductionAndroidExperiencePort(
            service=scoped_service,
            profiles=profiles,
            kernel=kernel,
            runtime_metadata=runtime_metadata,
        )
    handler = AndroidUiAgentV1Handler(
        canonical=canonical,
        observations=observations,
        planner=roles,
        actor=roles,
        action_verifier=DigestActionVerifier(),
        semantic_verifier=EvidenceBoundSemanticVerifier(
            TrustedTreeSemanticRole(),
            model_version=str(getattr(role_model, "model", "unknown")),
            prompt_version="android-ui-semantic-v1",
            grounding=grounding,
        ),
        store=step_store,
        dispatch=dispatch,
        experience=experience,
    )
    return ProductionAndroidUiComposition(
        runner=ProductionAndroidUiRunner(
            handler,
            ProductionCriteriaProvider(step_store, caller),
            observations,
        ),
        step_store=step_store,
        command_store=command_store,
        grounding=grounding,
        experience=experience,
        verification_attestor=verification_attestor,
    )


def _parse_ui_tree(content: bytes, screen_size: tuple[int, int]) -> tuple[UiNode, ...]:
    if not content or len(content) > _UI_TREE_LIMIT:
        return ()
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return ()
    width, height = screen_size
    nodes: list[UiNode] = []
    element_index = 0

    def visit(element: ElementTree.Element, ancestor_clickable: bool = False) -> None:
        nonlocal element_index
        if len(nodes) >= _UI_NODE_LIMIT:
            return
        index = element_index
        element_index += 1
        attributes = element.attrib
        directly_clickable = str(attributes.get("clickable") or "false").casefold() == "true"
        navigation_context = ancestor_clickable or directly_clickable
        raw_text = str(attributes.get("text") or attributes.get("content-desc") or "").strip()
        if raw_text:
            text = sanitize_summary(raw_text, maximum=300)
            if text and text != "[redacted]":
                bounds = _normalized_bounds(str(attributes.get("bounds") or ""), width, height)
                class_name = str(attributes.get("class") or "")
                resource_name = str(
                    attributes.get("resource-id") or ""
                ).rsplit("/", 1)[-1]
                if navigation_context:
                    semantic_kind = "navigation"
                    marker = None
                elif (
                    bounds is not None
                    and bounds[1] <= 0.25
                    and (
                        class_name.endswith("TextView")
                        or resource_name in _ANDROID_PAGE_TITLE_RESOURCES
                    )
                ):
                    semantic_kind = "page_title"
                    marker = evidence_marker("page_title", text)
                elif attributes.get("content-desc") and not class_name.endswith("TextView"):
                    semantic_kind = "container"
                    marker = evidence_marker("container", text)
                else:
                    semantic_kind = "state"
                    marker = evidence_marker("state", text)
                identity = json.dumps(
                    [attributes.get("resource-id"), attributes.get("bounds"), class_name, index],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                nodes.append(UiNode(
                    opaque_node_id(identity), text, bounds, navigation_context,
                    semantic_kind, marker,
                ))
        for child in element:
            visit(child, navigation_context)

    visit(root)
    return tuple(nodes)


def _normalized_bounds(
    value: str, width: int, height: int,
) -> tuple[float, float, float, float] | None:
    match = _BOUNDS.fullmatch(value)
    if match is None or width < 1 or height < 1:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    if not (0 <= left <= right <= width and 0 <= top <= bottom <= height):
        return None
    return (
        round(left / width, 6), round(top / height, 6),
        round(right / width, 6), round(bottom / height, 6),
    )


def _device_state_digest(device_state: Any) -> str:
    payload = json.dumps(
        {
            "foreground_app": device_state.foreground_app,
            "screen_size": device_state.screen_size,
            "orientation": str(device_state.orientation),
            "keyboard": str(device_state.keyboard_state),
            "connection": str(device_state.connection_state),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _GoalClause:
    index: int
    text: str
    start: int
    end: int


def _goal_clauses(goal: str) -> tuple[_GoalClause, ...]:
    """Deterministically split the canonical safe goal into exact quotes."""

    clauses: list[_GoalClause] = []
    start = 0
    segments: list[tuple[int, int]] = []
    for match in _GOAL_CLAUSE_DELIMITER.finditer(goal):
        segments.append((start, match.start()))
        start = match.end()
    segments.append((start, len(goal)))
    for raw_start, raw_end in segments:
        segment = goal[raw_start:raw_end]
        left_trimmed = segment.lstrip()
        text = left_trimmed.rstrip()
        if not _normalized_semantic_text(text):
            continue
        actual_start = raw_start + (len(segment) - len(left_trimmed))
        clauses.append(_GoalClause(
            len(clauses), text, actual_start, actual_start + len(text),
        ))
    return tuple(clauses)


def _observable_navigation_goal_clauses(goal: str) -> tuple[_GoalClause, ...]:
    """Keep terminal coverage scoped to navigation targets a UI can expose.

    The canonical goal remains intact for ownership, planning, and safety
    controls.  Criteria, however, can only prove a visible terminal target;
    clauses such as "do not change settings" or "do not go back" constrain
    execution but are not page titles, containers, or state labels.
    """

    return tuple(
        clause for clause in _goal_clauses(goal)
        if (
            _has_navigation_prefix(clause.text)
            and not _has_unsupported_state_mutation(clause.text)
            and _semantic_target_core(clause.text) is not None
        )
    )


def _ordered_waypoint_clauses(
    goal: str, clauses: tuple[_GoalClause, ...],
) -> tuple[_GoalClause, ...]:
    """Select explicit route prerequisites without naming any application/page.

    The grammar intentionally requires an ordering cue *and* at least two
    observable navigation clauses.  A list of unrelated destinations therefore
    remains the established single-terminal contract instead of gaining a
    guessed history requirement.
    """

    if len(clauses) < 2:
        return ()
    normalized = unicodedata.normalize("NFKC", goal).casefold()
    # Parallel/alternative requests are not an ordered route merely because a
    # later reporting or safety clause contains a temporal word.
    if re.search(r"(?:或者|或是|\bor\b|同时|并行|\bparallel\b)", normalized):
        return ()
    ordered: list[_GoalClause] = []
    for left, right in zip(clauses, clauses[1:]):
        bridge = normalized[left.end:right.start]
        right_prefix = normalized[right.start:min(len(normalized), right.start + 12)]
        if not (
            re.search(r"(?:\bthen\b|\bafter\b|然后|接着|后再|之后再)", bridge)
            or re.match(r"(?:再(?:次)?|然后|接着|最后|\bthen\b)", right_prefix)
            or re.match(r"(?:先|\bfirst\b|\bfrom\b)", normalized[left.start:left.end])
        ):
            return ()
        ordered.append(left)
    return tuple(ordered)


def _normalized_semantic_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _semantic_value_proves_clause(semantic_value: str, clause: str) -> bool:
    """Fail closed unless a terminal marker preserves the complete target.

    v1 has no authority for free-form semantic entailment.  It may remove one
    allowlisted leading action phrase and one allowlisted trailing generic UI
    noun from *both* values, then requires exact equality.  Everything in the
    remaining target/state -- including app, account, object and other entity
    tokens -- must survive.  A future versioned adapter ontology can expand
    this deliberately; synonym-only mappings remain recoverable for now.
    """

    if _has_unsupported_state_mutation(clause):
        return False
    source = _semantic_target_core(clause)
    marker = _semantic_target_core(semantic_value)
    return source is not None and marker is not None and marker == source


def _has_unsupported_state_mutation(value: str) -> bool:
    """Reject text-only mutation goals that need typed state evidence."""

    if not isinstance(value, str):
        return True
    current = unicodedata.normalize("NFKC", value).casefold().strip(
        _SEMANTIC_EDGE_PUNCTUATION
    )
    chinese_compact = re.sub(r"\s+", "", current)
    # A constrained "click to enter [page]" phrase is navigation, not a
    # request to mutate a setting.  It remains accepted only when its target
    # ends in a generic visible UI container, exactly like the guarded
    # "open [page]" grammar below.
    for prefix in ("再次点击进入", "点击进入"):
        if chinese_compact.startswith(prefix):
            target = _strip_trailing_system_settings_route(
                chinese_compact[len(prefix):]
            )
            return not _ends_with_generic_ui_noun(
                target.strip(_SEMANTIC_EDGE_PUNCTUATION)
            )
    if any(
        chinese_compact.startswith(prefix)
        for prefix in _UNSUPPORTED_STATE_PREFIXES_ZH
    ):
        return True
    if any(_starts_english_phrase(current, prefix) for prefix in _UNSUPPORTED_STATE_PREFIXES_EN):
        return True

    # Chinese/English "open" can mean either navigation or enablement.  v1
    # accepts it only when the target syntactically ends in a generic UI
    # container noun (for example, "打开微信通知设置").  Otherwise it remains
    # unavailable rather than guessing which meaning the user intended.
    for prefix in ("请打开", "打开"):
        if chinese_compact.startswith(prefix):
            target = _strip_trailing_system_settings_route(
                chinese_compact[len(prefix):]
            )
            return not _ends_with_generic_ui_noun(
                target.strip(_SEMANTIC_EDGE_PUNCTUATION)
            )
    for prefix in ("please open", "open"):
        if _starts_english_phrase(current, prefix):
            target = _strip_trailing_system_settings_route(current[len(prefix):])
            return not _ends_with_generic_ui_noun(
                target.strip(_SEMANTIC_EDGE_PUNCTUATION)
            )
    return False


def _has_navigation_prefix(value: str) -> bool:
    """Return whether a clause explicitly asks to navigate to a UI target."""

    if not isinstance(value, str):
        return False
    current = unicodedata.normalize("NFKC", value).casefold().strip(
        _SEMANTIC_EDGE_PUNCTUATION
    )
    current = _ROUTE_EXECUTION_PREFIX_ZH.sub("", current).strip(
        _SEMANTIC_EDGE_PUNCTUATION,
    )
    chinese_compact = re.sub(r"\s+", "", current)
    if any(chinese_compact.startswith(prefix) for prefix in _LEADING_ACTION_PREFIXES_ZH):
        return True
    return any(
        _starts_english_phrase(current, prefix)
        for prefix in _LEADING_ACTION_PREFIXES_EN
    )


def _starts_english_phrase(value: str, phrase: str) -> bool:
    if not value.startswith(phrase):
        return False
    boundary = len(phrase)
    return boundary == len(value) or not value[boundary].isalnum()


def _ends_with_generic_ui_noun(value: str) -> bool:
    if any(value.endswith(suffix) for suffix in _TRAILING_UI_NOUNS_ZH):
        return True
    for suffix in _TRAILING_UI_NOUNS_EN:
        if not value.endswith(suffix):
            continue
        boundary = len(value) - len(suffix)
        if boundary == 0 or not value[boundary - 1].isalnum():
            return True
    return False


def _semantic_target_core(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    current = unicodedata.normalize("NFKC", value).casefold().strip(
        _SEMANTIC_EDGE_PUNCTUATION
    )
    if not current:
        return None
    current = _ROUTE_EXECUTION_PREFIX_ZH.sub("", current).strip(
        _SEMANTIC_EDGE_PUNCTUATION,
    )
    if not current:
        return None

    # Explicit route connectors (for example ``先`` / ``再``) may precede a
    # normal navigation verb.  Strip at most two allowlisted prefixes so the
    # target remains exact while the compiler does not mistake route grammar
    # for a visible page title.
    for _ in range(2):
        removed = False
        for prefix in _LEADING_ACTION_PREFIXES_ZH:
            if current.startswith(prefix):
                current = current[len(prefix):].strip(_SEMANTIC_EDGE_PUNCTUATION)
                removed = True
                break
        if removed:
            continue
        for prefix in _LEADING_ACTION_PREFIXES_EN:
            if not current.startswith(prefix):
                continue
            boundary = len(prefix)
            if boundary < len(current) and current[boundary].isalnum():
                continue
            current = current[boundary:].strip(_SEMANTIC_EDGE_PUNCTUATION)
            removed = True
            break
        if not removed:
            break

    for prefix in _GENERIC_DEVICE_TARGET_PREFIXES_ZH:
        if current.startswith(prefix):
            current = current[len(prefix):].strip(_SEMANTIC_EDGE_PUNCTUATION)
            break

    # DSH may retain a parenthesized System Settings navigation route after
    # the terminal target, for example ``通知设置（打开系统设置 → 通知）``.
    # The route constrains how planning reaches the target; it is not a second
    # string that can remain visible in the terminal Android title. Keep the
    # accepted syntax narrow and strip it only for marker equivalence.
    current = _strip_trailing_system_settings_route(current).strip(
        _SEMANTIC_EDGE_PUNCTUATION
    )
    # DSH sometimes appends an English localization alias directly after a
    # Chinese page label (for example, ``通知（Notifications）设置页面``).
    # It is not a second visible target on this Chinese system image, so strip
    # only that tightly delimited ASCII alias before matching the title.
    current = _INLINE_ENGLISH_UI_ALIAS.sub("", current)

    for suffix in _TRAILING_UI_NOUNS_ZH:
        if current.endswith(suffix):
            current = current[:-len(suffix)].strip(_SEMANTIC_EDGE_PUNCTUATION)
            break
    else:
        for suffix in _TRAILING_UI_NOUNS_EN:
            if not current.endswith(suffix):
                continue
            boundary = len(current) - len(suffix)
            if boundary > 0 and current[boundary - 1].isalnum():
                continue
            current = current[:boundary].strip(_SEMANTIC_EDGE_PUNCTUATION)
            break

    normalized = _normalized_semantic_text(current)
    return normalized if len(normalized) >= 2 else None


def _strip_trailing_system_settings_route(value: str) -> str:
    current = _TRAILING_SYSTEM_SETTINGS_ROUTE_ZH.sub("", value)
    return _TRAILING_SYSTEM_SETTINGS_ROUTE_EN.sub("", current)


def _has_generic_device_locator(value: str) -> bool:
    if not isinstance(value, str):
        return False
    current = unicodedata.normalize("NFKC", value).casefold().strip(
        _SEMANTIC_EDGE_PUNCTUATION
    )
    for prefix in _LEADING_ACTION_PREFIXES_ZH:
        if current.startswith(prefix):
            current = current[len(prefix):].strip(_SEMANTIC_EDGE_PUNCTUATION)
            break
    return any(
        current.startswith(prefix)
        for prefix in _GENERIC_DEVICE_TARGET_PREFIXES_ZH
    )


def _authenticated_scope_for_observation(
    *, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
    source: Any, profiles: Any, runtime_metadata: Any | None = None,
) -> AuthenticatedAndroidScope:
    if (
        snapshot.runner_kind != RUNNER_KIND
        or snapshot.runner_version != RUNNER_VERSION
        or snapshot.profile_id is None
        or snapshot.profile_generation is None
        or snapshot.boot_id is None
        or snapshot.canonical_device_id is None
        or snapshot.runner_binding_id is None
        or criteria.revision != snapshot.revision
        or source.task_id != snapshot.task_id
        or source.device_id != snapshot.canonical_device_id
    ):
        raise ProductionAndroidUiError("android_ui_experience_scope_crosswire")
    profile = profiles.require_profile(
        principal_id=snapshot.owner.principal_id,
        controller_id=snapshot.owner.controller_id,
        profile_id=snapshot.profile_id,
    )
    fingerprint = profile.fingerprint
    if (
        profile.owner_principal_id != snapshot.owner.principal_id
        or profile.owner_controller_id != snapshot.owner.controller_id
        or profile.profile_id != snapshot.profile_id
        or profile.profile_generation != snapshot.profile_generation
        or profile.boot_id != snapshot.boot_id
        or profile.canonical_device_id != snapshot.canonical_device_id
        or fingerprint is None
    ):
        raise ProductionAndroidUiError("android_ui_experience_profile_crosswire")
    width, height = source.device_state.screen_size
    orientation = getattr(
        source.device_state.orientation, "value", str(source.device_state.orientation),
    )
    app_package = source.device_state.foreground_app or "unknown"
    if (
        not isinstance(app_package, str)
        or len(app_package) > 255
        or not _ANDROID_PACKAGE.fullmatch(app_package)
    ):
        app_package = "unknown"
    if orientation not in {"portrait", "landscape"}:
        orientation = "unknown"
    app_version = "unknown"
    android_build = "unknown"
    read_metadata = getattr(runtime_metadata, "read", None)
    if callable(read_metadata) and app_package != "unknown":
        try:
            metadata = read_metadata(profile=profile, app_package=app_package)
            if (
                metadata is not None
                and getattr(metadata, "app_package", None) == app_package
                and isinstance(getattr(metadata, "app_version_digest", None), str)
                and _SHA256.fullmatch(metadata.app_version_digest)
                and isinstance(getattr(metadata, "android_build_digest", None), str)
                and _SHA256.fullmatch(metadata.android_build_digest)
                and fingerprint.android_build_digest == metadata.android_build_digest
            ):
                app_version = metadata.app_version_digest
                android_build = metadata.android_build_digest
        except Exception:
            # K3 is advisory: metadata timeout, unavailability, or Profile/build
            # drift remains task-local/cold and never changes the K2 Task outcome.
            pass
    return authenticated_android_scope(
        principal_id=snapshot.owner.principal_id,
        controller_id=snapshot.owner.controller_id,
        profile_id=snapshot.profile_id,
        profile_generation=snapshot.profile_generation,
        app_package=app_package,
        app_version=app_version,
        android_api=str(fingerprint.api_level),
        android_build=android_build,
        resolution=f"{width}x{height}",
        density=(str(fingerprint.density) if fingerprint.density is not None else "unknown"),
        orientation=str(orientation),
        observation_schema=_OBSERVATION_SCHEMA,
        runner_kind=RUNNER_KIND,
        runner_version=RUNNER_VERSION,
        criteria_revision=criteria.revision,
        criteria_digest=criteria.digest,
        task_id=snapshot.task_id,
        subtask_id=snapshot.runner_binding_id,
    )


def _immutable_record_matches(
    *, record: Mapping[str, Any], snapshot: CanonicalSnapshot,
    criteria: CriteriaRevision, source: Any, step_index: int,
) -> bool:
    after = record.get("after")
    if not isinstance(after, Mapping):
        return False
    ui_tree_digest = (
        source.ui_tree.artifact.sha256
        if source.ui_tree.artifact is not None else None
    )
    expected_core = {
        "task_id": snapshot.task_id,
        "runner_kind": RUNNER_KIND,
        "runner_version": RUNNER_VERSION,
        "revision": snapshot.revision,
        "criteria_digest": criteria.digest,
        "goal_digest": criteria.goal_digest,
        "coverage_digest": criteria.coverage_digest,
        "latest_step_index": step_index,
        "owner_scope_digest": owner_scope_digest(snapshot.owner),
    }
    if any(record.get(key) != value for key, value in expected_core.items()):
        return False
    expected_after = {
        # GoalVerificationRecord.private_durable_record deliberately stores
        # binding identifiers as domain-separated digests.  Comparing those
        # durable audit fields with raw canonical identifiers makes every
        # otherwise-valid K2 terminal proof fail closed after a restart.
        "profile_id_digest": opaque_digest("profile-id", snapshot.profile_id),
        "profile_generation": snapshot.profile_generation,
        "boot_id_digest": opaque_digest("boot-id", snapshot.boot_id),
        "canonical_device_id_digest": opaque_digest(
            "canonical-device-id", snapshot.canonical_device_id,
        ),
        "screenshot_digest": source.screenshot.artifact.sha256,
        "ui_tree_digest": ui_tree_digest,
        "device_state_digest": _device_state_digest(source.device_state),
        "freshness_digest": opaque_digest("observation-freshness", source.id),
    }
    if any(after.get(key) != value for key, value in expected_after.items()):
        return False
    verdicts = record.get("verdicts")
    return (
        record.get("overall") in {"satisfied", "unsatisfied"}
        and record.get("already_satisfied") is False
        and isinstance(verdicts, list)
        and bool(verdicts)
        and all(
            isinstance(item, Mapping)
            and item.get("state") in {"satisfied", "unsatisfied"}
            for item in verdicts
        )
    )


def _verification_record_id(
    scope: AuthenticatedAndroidScope, immutable_record: Mapping[str, Any],
) -> str:
    payload = json.dumps(
        immutable_record,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "record_" + hashlib.sha256(
        (scope.attestation_digest + "\x00" + payload).encode("utf-8")
    ).hexdigest()


def _experience_semantic_anchor(criteria: CriteriaRevision) -> str:
    marker_kinds = sorted({
        marker.split(":", 1)[0]
        for criterion in criteria.criteria
        for marker in criterion.required_evidence_markers
    })
    suffix = "-".join(marker_kinds) if marker_kinds else "observable"
    return f"frozen {suffix} criterion"


def _kernel_payload(
    intent: AndroidUiActionIntent, screen_size: tuple[int, int],
) -> dict[str, object]:
    width, height = screen_size
    arguments = dict(intent.action.arguments)

    def x(name: str) -> int:
        return round(float(arguments[name]) * max(0, width - 1))

    def y(name: str) -> int:
        return round(float(arguments[name]) * max(0, height - 1))

    kind = intent.action.kind
    if kind in {"tap", "long_press"}:
        return {"x": x("x"), "y": y("y")}
    if kind == "swipe":
        return {
            "start_x": x("x"), "start_y": y("y"),
            "end_x": x("end_x"), "end_y": y("end_y"),
        }
    if kind == "input_text":
        return {"text": str(arguments["text"])}
    if kind == "open_app":
        return {"package": str(arguments["package"])}
    if kind in {"back", "home", "recents"}:
        return {}
    raise GenericCommandConflict("unsupported production Android UI primitive")


def _validate_kernel_payload(
    stored: Mapping[str, Any], supplied: Mapping[str, object], command_type: str,
) -> None:
    if command_type == "input_text":
        # Plaintext lives only in RuntimeKernel's transient envelope.  Its two
        # durable ledgers independently retain only opaque redacted digests.
        if set(supplied) != {"text"}:
            raise ProductionAndroidUiError("android_ui_typed_payload_invalid")
        return
    if dict(stored) != dict(supplied):
        raise ProductionAndroidUiError("android_ui_physical_payload_crosswire")


def _command_id(snapshot: CanonicalSnapshot, intent: AndroidUiActionIntent) -> str:
    payload = json.dumps(
        [
            snapshot.task_id,
            snapshot.runner_binding_id,
            RUNNER_KIND,
            RUNNER_VERSION,
            intent.action_intent_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _step_id(task_id: str, intent: AndroidUiActionIntent) -> str:
    return "step-" + hashlib.sha256(
        f"{task_id}\x00{intent.criteria_revision}\x00{intent.step_index}".encode()
    ).hexdigest()


def _stage_id(task_id: str) -> str:
    return "android-ui-stage-" + hashlib.sha256(task_id.encode()).hexdigest()


def _task_for_action(kernel: RuntimeKernel, action_id: str) -> str:
    try:
        return str(kernel.load_action_by_id(action_id).task_id)
    except RecordNotFound as error:
        raise ProductionAndroidUiError("android_ui_kernel_action_missing") from error


def _as_timestamp(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = [
    "DigestActionVerifier",
    "ProductionAndroidCanonicalReader",
    "ProductionAndroidObservationProvider",
    "ProductionAndroidExperiencePort",
    "ProductionGenericEvidenceLookup",
    "ProductionKernelPhysicalDispatcher",
    "ProductionAndroidUiComposition",
    "ProductionAndroidUiError",
    "ProductionAndroidUiRunner",
    "ProductionAndroidVerificationAttestor",
    "ProductionCriteriaProvider",
    "ProductionModelArtifactLoader",
    "ProductionToolCaller",
    "TrustedTreeSemanticRole",
    "compose_production_android_ui_runner",
]
