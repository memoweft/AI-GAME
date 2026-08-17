from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .artifacts import ArtifactStore
from .domain import (
    DistilledTransition,
    EnvironmentFactory,
    GameProfile,
    LearningJob,
    OutcomeVerification,
    PolicyMemory,
    Trainer,
    Transition,
    TransportReceipt,
)
from .store import (
    LearningIdempotencyConflict,
    LearningRecordNotFound,
    LearningStore,
    LearningStoreError,
)


class GameLearningError(RuntimeError):
    def __init__(
        self, *, code: str, public_message: str, status_code: int = 409
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.status_code = status_code

    def as_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.public_message}}


@dataclass(frozen=True, slots=True)
class _ActiveWork:
    future: Future[None]
    cancelled: threading.Event


class GameLearner:
    """Deep learning Module over injected environment and persistence Adapters."""

    def __init__(
        self,
        *,
        store: LearningStore,
        artifacts: ArtifactStore,
        environment_factory: EnvironmentFactory | None,
        profiles: list[GameProfile] | tuple[GameProfile, ...] | None = None,
        trainer: Trainer | None = None,
        max_workers: int = 1,
        max_pending: int = 8,
    ) -> None:
        if max_workers < 1 or max_pending < 0:
            raise ValueError("invalid game learning worker bounds")
        selected_profiles = tuple(profiles or (_default_profile(),))
        if len({profile.profile_id for profile in selected_profiles}) != len(selected_profiles):
            raise ValueError("game profile ids must be unique")
        self.store = store
        self.artifacts = artifacts
        self.environment_factory = environment_factory
        self.trainer = trainer
        self._profiles = {profile.profile_id: profile for profile in selected_profiles}
        self._max_workers = max_workers
        self._slots = threading.BoundedSemaphore(max_workers + max_pending)
        self._state_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._active: dict[str, _ActiveWork] = {}
        self._running = False

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            self.store.initialize()
            self.store.recover_active_jobs()
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="ai-game-learning",
            )
            self._running = True

    def shutdown(self, *, wait: bool = True) -> None:
        with self._state_lock:
            if not self._running and self._executor is None:
                return
            self._running = False
            executor = self._executor
            active = list(self._active.items())
        for job_id, work in active:
            work.cancelled.set()
            try:
                self.store.request_stop(job_id)
            except LearningRecordNotFound:
                pass
            work.future.cancel()
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
        with self._state_lock:
            self._executor = None
            self._active.clear()

    def list_profiles(self) -> list[GameProfile]:
        return sorted(self._profiles.values(), key=lambda item: item.profile_id)

    def learn(
        self,
        instruction: str,
        client_request_id: str,
        profile_id: str = "stzb-tutorial-v1",
        target_id: str | None = None,
    ) -> LearningJob:
        self._require_running()
        clean_instruction = instruction.strip()
        clean_request_id = client_request_id.strip()
        if not clean_instruction or len(clean_instruction) > 200:
            raise GameLearningError(
                code="learning_instruction_invalid",
                public_message="学习指令不能为空或超过长度限制。",
                status_code=422,
            )
        if not clean_request_id or len(clean_request_id) > 200:
            raise GameLearningError(
                code="learning_request_id_invalid",
                public_message="client_request_id 无效。",
                status_code=422,
            )
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise GameLearningError(
                code="game_profile_not_found",
                public_message="未找到指定的游戏学习配置。",
                status_code=404,
            )
        resolved_target = target_id or profile.default_target_id
        digest = _request_digest(
            instruction=clean_instruction,
            profile_id=profile.profile_id,
            target_id=resolved_target,
        )
        existing = self.store.get_job_by_request(clean_request_id)
        if existing is not None:
            if existing.request_digest != digest:
                raise GameLearningError(
                    code="learning_request_id_conflict",
                    public_message="同一 client_request_id 已用于不同学习请求。",
                    status_code=409,
                )
            return existing
        if self.environment_factory is None:
            raise GameLearningError(
                code="learning_environment_not_configured",
                public_message="游戏学习环境尚未配置。",
                status_code=409,
            )
        if not self._slots.acquire(blocking=False):
            raise GameLearningError(
                code="learning_queue_full",
                public_message="游戏学习队列已满，请稍后重试。",
                status_code=429,
            )

        job_id = str(uuid4())
        try:
            job, created = self.store.accept_job(
                job_id=job_id,
                profile_id=profile.profile_id,
                profile_revision=profile.revision,
                target_id=resolved_target,
                instruction=clean_instruction,
                client_request_id=clean_request_id,
                request_digest=digest,
            )
        except LearningIdempotencyConflict as exc:
            self._slots.release()
            raise GameLearningError(
                code="learning_request_id_conflict",
                public_message="同一 client_request_id 已用于不同学习请求。",
                status_code=409,
            ) from exc
        except Exception:
            self._slots.release()
            raise
        if not created:
            self._slots.release()
            return job

        cancelled = threading.Event()
        try:
            with self._state_lock:
                executor = self._executor if self._running else None
            if executor is None:
                raise RuntimeError("game learner stopped")
            future = executor.submit(self._run_job, job_id, profile, cancelled)
        except RuntimeError:
            self._slots.release()
            return self.store.finish(
                job_id,
                status="failed",
                detail="游戏学习协调器不可用。",
                error_code="learning_coordinator_unavailable",
            )

        work = _ActiveWork(future=future, cancelled=cancelled)
        with self._state_lock:
            self._active[job_id] = work
        future.add_done_callback(lambda done, current=job_id: self._future_done(current, done))
        return job

    def list_jobs(self, limit: int = 100) -> list[LearningJob]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise GameLearningError(
                code="learning_limit_invalid",
                public_message="limit 必须是 1 到 500 之间的整数。",
                status_code=422,
            )
        return self.store.list_jobs(limit)

    def inspect(self, job_id: str) -> LearningJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise GameLearningError(
                code="learning_job_not_found",
                public_message="未找到指定的学习任务。",
                status_code=404,
            )
        return job

    def stop(self, job_id: str) -> LearningJob:
        self._require_running()
        try:
            job = self.store.request_stop(job_id)
        except LearningRecordNotFound as exc:
            raise GameLearningError(
                code="learning_job_not_found",
                public_message="未找到指定的学习任务。",
                status_code=404,
            ) from exc
        with self._state_lock:
            active = self._active.get(job_id)
        if active is not None:
            active.cancelled.set()
            active.future.cancel()
        return job

    def _run_job(
        self,
        job_id: str,
        profile: GameProfile,
        cancelled: threading.Event,
    ) -> None:
        session = None
        open_transition_id: str | None = None
        try:
            job = self.store.claim_job(job_id)
            if job is None:
                return
            if self.environment_factory is None:  # pragma: no cover - learn invariant
                raise RuntimeError("learning environment unavailable")
            policy = self.store.get_policy(profile.profile_id)
            session = self.environment_factory.open(
                profile=profile,
                target_id=job.target_id,
                is_cancelled=cancelled.is_set,
            )
            deadline = time.monotonic() + profile.max_duration_seconds

            for sequence in range(1, profile.max_actions + 1):
                if cancelled.is_set():
                    self.store.finish(
                        job_id,
                        status="stopped",
                        detail="学习任务已在下一动作前停止。",
                        error_code=None,
                    )
                    return
                if time.monotonic() >= deadline:
                    self.store.finish(
                        job_id,
                        status="not_learned",
                        detail="学习任务达到时间预算，未确认目标成功。",
                        error_code="learning_time_budget_exhausted",
                    )
                    return

                before_observation = session.observe()
                before_ref = self.artifacts.put(
                    job_id=job_id,
                    label="before",
                    sequence=sequence,
                    observation=before_observation,
                )
                try:
                    proposal = session.propose_action(
                        instruction=job.instruction,
                        observation=before_observation,
                        policy_memory=policy,
                    )
                except Exception:
                    if cancelled.is_set():
                        self.store.finish(
                            job_id,
                            status="stopped",
                            detail="学习任务已在 Policy 提案期间停止。",
                            error_code=None,
                        )
                        return
                    raise
                if proposal.action is not None:
                    if proposal.action.action not in profile.allowed_actions:
                        self.store.finish(
                            job_id,
                            status="failed",
                            detail="模型提出了 GameProfile 不允许的动作，未传输。",
                            error_code="profile_action_not_allowed",
                        )
                        return
                    if job.target_id and proposal.action.target_id != job.target_id:
                        self.store.finish(
                            job_id,
                            status="failed",
                            detail="动作目标与学习任务绑定目标不一致，未传输。",
                            error_code="profile_target_mismatch",
                        )
                        return

                next_transition_id = str(uuid4())
                self.store.append_intent(
                    transition_id=next_transition_id,
                    job_id=job_id,
                    sequence=sequence,
                    proposal=proposal,
                    before=before_ref,
                )
                open_transition_id = next_transition_id
                if cancelled.is_set():
                    self._finalize_without_outcome(
                        open_transition_id,
                        receipt=TransportReceipt("not_sent", "cancelled before transport"),
                    )
                    open_transition_id = None
                    self.store.finish(
                        job_id,
                        status="stopped",
                        detail="学习任务已在动作传输前停止。",
                        error_code=None,
                    )
                    return

                if proposal.kind == "execute":
                    try:
                        receipt = session.execute(proposal.action)  # type: ignore[arg-type]
                    except Exception:
                        receipt = TransportReceipt(
                            "uncertain", "device transport raised before receipt"
                        )
                elif proposal.kind == "wait":
                    if cancelled.wait(proposal.wait_seconds or 0.0):
                        self._finalize_without_outcome(
                            open_transition_id,
                            receipt=TransportReceipt("not_sent", "cancelled while waiting"),
                        )
                        open_transition_id = None
                        self.store.finish(
                            job_id,
                            status="stopped",
                            detail="学习任务在等待期间停止。",
                            error_code=None,
                        )
                        return
                    receipt = TransportReceipt("not_sent", "wait has no device input")
                else:
                    receipt = TransportReceipt("not_sent", "terminate is only a proposal")

                if receipt.status == "uncertain":
                    self._finalize_without_outcome(open_transition_id, receipt=receipt)
                    open_transition_id = None
                    self.store.finish(
                        job_id,
                        status="stopped_uncertain",
                        detail="动作传输结果不确定；任务已停止且不会重放动作。",
                        error_code="transport_uncertain",
                    )
                    return
                if cancelled.is_set() and receipt.status == "accepted":
                    self._finalize_without_outcome(open_transition_id, receipt=receipt)
                    open_transition_id = None
                    self.store.finish(
                        job_id,
                        status="stopped_uncertain",
                        detail="停止请求与已接受动作并发，动作结果尚未验证。",
                        error_code="stop_after_transport",
                    )
                    return

                after_observation = None
                after_ref = None
                if receipt.status != "rejected":
                    try:
                        after_observation = session.observe()
                        after_ref = self.artifacts.put(
                            job_id=job_id,
                            label="after",
                            sequence=sequence,
                            observation=after_observation,
                        )
                    except Exception:
                        self._finalize_without_outcome(open_transition_id, receipt=receipt)
                        open_transition_id = None
                        uncertain = receipt.status == "accepted"
                        self.store.finish(
                            job_id,
                            status="stopped_uncertain" if uncertain else "failed",
                            detail="动作后证据无法取得；未确认结果且不会重放动作。",
                            error_code="post_action_observation_failed",
                        )
                        return
                try:
                    outcome = session.verifier.verify(
                        before=before_observation,
                        action=proposal.action,
                        transport=receipt,
                        after=after_observation,
                    )
                except Exception:
                    self._finalize_without_outcome(open_transition_id, receipt=receipt)
                    open_transition_id = None
                    self.store.finish(
                        job_id,
                        status=(
                            "stopped_uncertain"
                            if receipt.status == "accepted"
                            else "failed"
                        ),
                        detail="OutcomeVerifier 未能产生可信结果。",
                        error_code="outcome_verifier_failed",
                    )
                    return
                finalized = self.store.finalize_transition(
                    transition_id=open_transition_id,
                    after=after_ref,
                    transport=receipt,
                    outcome=outcome,
                )
                open_transition_id = None

                verifier_kind = outcome.detail.partition(":")[0]
                if verifier_kind in {"unsafe", "failed"} or (
                    outcome.confirmed and outcome.reward < 0
                ):
                    self.store.finish(
                        job_id,
                        status="failed" if verifier_kind == "unsafe" else "not_learned",
                        detail="OutcomeVerifier 已确认失败或安全范围越界，任务立即终止。",
                        error_code=(
                            "unsafe_scene" if verifier_kind == "unsafe" else "confirmed_failure"
                        ),
                    )
                    return

                if cancelled.is_set():
                    self.store.finish(
                        job_id,
                        status="stopped",
                        detail="学习任务已停止；最后动作结果已明确记录。",
                        error_code=None,
                    )
                    return
                if outcome.task_succeeded:
                    transitions = self.store.list_transitions(job_id)
                    distilled = self._distill(profile, transitions, policy)
                    if distilled:
                        self.store.complete_learned(
                            job_id,
                            distilled=distilled,
                            detail="任务成功已由 OutcomeVerifier 确认，正向轨迹已原子晋升。",
                        )
                    else:
                        self.store.finish(
                            job_id,
                            status="not_learned",
                            detail="任务成功已确认，但没有可晋升的正向物理 Transition。",
                            error_code="no_positive_transition",
                        )
                    return
                if proposal.kind == "terminate":
                    self.store.finish(
                        job_id,
                        status="not_learned",
                        detail="模型提出结束，但 OutcomeVerifier 未确认任务成功。",
                        error_code="termination_unconfirmed",
                    )
                    return
                if receipt.status == "rejected":
                    self.store.finish(
                        job_id,
                        status="failed",
                        detail="设备明确拒绝了动作。",
                        error_code="transport_rejected",
                    )
                    return
                policy = self.store.get_policy(profile.profile_id)

            self.store.finish(
                job_id,
                status="not_learned",
                detail="学习任务达到动作预算，未确认目标成功。",
                error_code="learning_action_budget_exhausted",
            )
        except Exception as exc:
            if cancelled.is_set() and open_transition_id is None:
                status = "stopped"
                detail = "学习任务已合作式停止。"
                error_code = None
            else:
                status = "stopped_uncertain" if open_transition_id is not None else "failed"
                candidate_code = getattr(exc, "code", None)
                candidate_message = getattr(exc, "public_message", None)
                if open_transition_id is not None:
                    detail = "学习任务在已记录动作意图后异常；为防止重放，结果标记为不确定。"
                    error_code = "learning_interrupted_after_intent"
                elif isinstance(candidate_code, str) and candidate_code.startswith(
                    (
                        "game_",
                        "learning_",
                        "profile_",
                        "target_",
                        "foreground_",
                        "scene_",
                        "task_",
                        "instruction_",
                        "executor_",
                        "policy_",
                        "local_evidence_",
                        "evidence_",
                        "outcome_",
                        "transport_",
                        "unsafe_",
                    )
                ):
                    error_code = candidate_code[:100]
                    detail = (
                        candidate_message[:1_000]
                        if isinstance(candidate_message, str) and candidate_message
                        else "游戏学习预检失败。"
                    )
                else:
                    detail = "游戏学习处理失败。"
                    error_code = "learning_failed"
            try:
                self.store.finish(
                    job_id,
                    status=status,
                    detail=detail,
                    error_code=error_code,
                )
            except Exception:
                pass
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _finalize_without_outcome(
        self, transition_id: str, *, receipt: TransportReceipt
    ) -> None:
        self.store.finalize_transition(
            transition_id=transition_id,
            after=None,
            transport=receipt,
            outcome=OutcomeVerification(
                confirmed=False,
                task_succeeded=False,
                reward=0.0,
                detail="outcome not confirmed",
            ),
        )

    def _distill(
        self,
        profile: GameProfile,
        transitions: list[Transition],
        previous: PolicyMemory,
    ) -> tuple[DistilledTransition, ...]:
        eligible = tuple(
            DistilledTransition(
                action=item.proposal.action,
                reward=item.outcome.reward,
                before_sha256=item.before.sha256 if item.before else None,
                after_sha256=item.after.sha256 if item.after else None,
                verifier_detail=item.outcome.detail,
            )
            for item in transitions
            if item.finalized
            and item.proposal.action is not None
            and item.transport is not None
            and item.transport.status == "accepted"
            and item.outcome is not None
            and item.outcome.confirmed
            and item.outcome.reward > 0
        )
        if self.trainer is not None:
            candidates = self.trainer.distill(
                profile=profile,
                transitions=transitions,
                previous=previous,
            )
            if any(item not in eligible for item in candidates):
                raise LearningStoreError(
                    "trainer returned a trajectory item without eligible Transition provenance"
                )
            return tuple(candidates)
        return eligible

    def _future_done(self, job_id: str, future: Future[None]) -> None:
        with self._state_lock:
            self._active.pop(job_id, None)
        self._slots.release()
        try:
            future.exception()
        except BaseException:
            pass

    def _require_running(self) -> None:
        with self._state_lock:
            running = self._running
        if not running:
            raise GameLearningError(
                code="learning_coordinator_unavailable",
                public_message="游戏学习协调器尚未启动或正在关闭。",
                status_code=503,
            )


def _request_digest(
    *, instruction: str, profile_id: str, target_id: str | None
) -> str:
    payload: dict[str, Any] = {
        "instruction": instruction,
        "profile_id": profile_id,
        "target_id": target_id,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _default_profile() -> GameProfile:
    return GameProfile(
        profile_id="stzb-tutorial-v1",
        name="率土之滨低频教程与菜单导航",
        allowed_actions=("tap", "keyevent", "swipe"),
        max_actions=25,
        max_duration_seconds=180.0,
    )
