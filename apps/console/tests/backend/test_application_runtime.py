from __future__ import annotations

import threading
import time
import sqlite3
from pathlib import Path

import pytest

from ai_game_console.application_runtime import (
    ApplicationRuntime, ApplicationRuntimeError, Decision, ExecutionReceipt,
    ExecutionReconciliation, Input, InstanceStatus, Intent, MemoryCandidate,
    Observation, Outcome, OutcomeStatus, Pause, Resume, RuntimeEvent,
    RetryableApplicationError, RuntimeOutcome, Stop,
)
from ai_game_console.application_runtime.store import _SQLiteApplicationStore


def test_public_contract_exports_instance_nested_types_and_status_aliases():
    assert ApplicationRuntimeError.code == "application_runtime_error"
    assert RuntimeEvent.__name__ == "RuntimeEvent"
    assert RuntimeOutcome.__name__ == "RuntimeOutcome"
    assert InstanceStatus is not None
    assert OutcomeStatus is not None


def test_deferred_reconciliation_contract_is_explicit_and_bounded():
    deferred=ExecutionReconciliation(
        Outcome("unconfirmed","owner action in flight",terminal=False),
        "owner-inspect-active",
        ExecutionReceipt("owner-1",False,"active_dispatch"),
        retry_after_seconds=1.0,
    )
    assert deferred.retry_after_seconds == 1.0
    with pytest.raises(ValueError):
        ExecutionReconciliation(
            Outcome("confirmed_success",terminal=False),
            "invalid-active",
            retry_after_seconds=1.0,
        )

    assert RetryableApplicationError(0.2).wait_seconds == 0.2
    with pytest.raises(ValueError):
        RetryableApplicationError(0.1)
    with pytest.raises(ValueError):
        ExecutionReconciliation(
            Outcome("unconfirmed",terminal=False),
            "invalid-delay",
            retry_after_seconds=0.1,
        )


def wait_for(runtime: ApplicationRuntime, instance_id: str, predicate, timeout: float = 1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = runtime.inspect(instance_id)
        if predicate(value): return value
        time.sleep(0.01)
    raise AssertionError(runtime.inspect(instance_id))


class Observations:
    def __init__(self, *, fresh_after: bool = True): self.calls = 0; self.fresh_after = fresh_after
    def observe(self, instance):
        self.calls += 1
        return Observation(f"obs-{self.calls}", fresh=self.calls == 1 or self.fresh_after)


class Owner:
    def __init__(self): self.reservations=[]; self.dispatches=[]
    def reserve(self, instance, intent): self.reservations.append(intent.name); return f"reserve-{len(self.reservations)}"
    def dispatch(self, reservation_id, instance, intent): self.dispatches.append((reservation_id,intent.name)); return ExecutionReceipt("receipt", True)


class SuccessVerifier:
    def verify(self, context): return Outcome("confirmed_success", "fresh verified evidence")


class OneActionPolicy:
    def __init__(self, *, candidate=None): self.candidate=candidate; self.contexts=[]
    def decide(self, context): self.contexts.append(context); return Decision(Intent("safe-action"), memory_candidate=self.candidate)


def test_success_requires_fresh_after_and_promotes_only_through_gate(tmp_path: Path):
    candidate=MemoryCandidate("general-v1", {"hint":"verified"}, ("fresh verified evidence",))
    gate_calls=[]
    class Gate:
        def promote(self, received, outcome): gate_calls.append((received,outcome)); return True
    owner=Owner(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=OneActionPolicy(candidate=candidate), execution_owner=owner, verifier=SuccessVerifier(), memory_gate=Gate())
    instance=runtime.start("general-v1","start-1",initial_input="go")
    done=wait_for(runtime,instance.instance_id,lambda x:x.terminal)
    runtime.shutdown()
    assert done.status == "completed" and done.memory_version == 1
    assert owner.reservations == ["safe-action"] and len(owner.dispatches) == 1
    assert len(gate_calls) == 1 and done.outcomes[-1].status == "confirmed_success"


def test_nonfresh_after_cannot_confirm_or_promote_memory(tmp_path: Path):
    candidate=MemoryCandidate("general-v1", {"hint":"no"}, ("not fresh",))
    class Gate:
        def promote(self, candidate, outcome): raise AssertionError("memory gate must not run")
    runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(fresh_after=False), policy=OneActionPolicy(candidate=candidate), execution_owner=Owner(), verifier=SuccessVerifier(), memory_gate=Gate())
    instance=runtime.start("general-v1","start-1")
    degraded=wait_for(runtime,instance.instance_id,lambda x:x.terminal)
    runtime.shutdown()
    assert degraded.status == "failed" and degraded.memory_version == 0
    assert degraded.outcomes[-1].status == "uncertain"


def test_input_before_intent_fences_old_decision_and_automatically_uses_new_revision(tmp_path: Path):
    entered=threading.Event(); release=threading.Event()
    class Policy:
        def __init__(self): self.calls=0
        def decide(self, context):
            self.calls += 1
            if self.calls == 1: entered.set(); assert release.wait(1)
            return Decision(Intent("action-after-input"))
    policy=Policy(); owner=Owner(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=policy, execution_owner=owner, verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1")
    assert entered.wait(1)
    amended=runtime.command(instance.instance_id,Input("revised"),"input-1")
    release.set()
    done=wait_for(runtime,instance.instance_id,lambda x:x.terminal)
    runtime.shutdown()
    assert amended.revision == 1 and done.status == "completed" and len(owner.dispatches) == 1


def test_dispatch_commit_fence_does_not_ack_pause_before_owner_returns(tmp_path: Path):
    entered=threading.Event(); release=threading.Event(); pause_returned=threading.Event()
    class BlockingOwner(Owner):
        def dispatch(self, reservation_id, instance, intent):
            entered.set(); assert release.wait(1)
            return super().dispatch(reservation_id,instance,intent)
    owner=BlockingOwner(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=OneActionPolicy(), execution_owner=owner, verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1"); assert entered.wait(1)
    result=[]
    def pause():
        result.append(runtime.command(instance.instance_id,Pause(),"pause-1")); pause_returned.set()
    thread=threading.Thread(target=pause); thread.start()
    assert not pause_returned.wait(0.08)
    release.set(); assert pause_returned.wait(1); thread.join()
    paused=wait_for(runtime,instance.instance_id,lambda x:x.status == "paused" and bool(x.outcomes))
    runtime.shutdown()
    assert result[0].status == "paused" and paused.outcomes[-1].status == "confirmed_success"


def test_wait_is_persisted_and_input_interrupts_timer_without_busy_loop(tmp_path: Path):
    class WaitingThenAction:
        def __init__(self): self.calls=0
        def decide(self, context):
            self.calls += 1
            if self.calls == 1: return Decision(wait_seconds=30)
            return Decision(Intent("after-input"))
    policy=WaitingThenAction(); owner=Owner(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=policy, execution_owner=owner, verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1")
    waiting=wait_for(runtime,instance.instance_id,lambda x:x.status == "waiting")
    assert policy.calls == 1 and waiting.wake_at is not None
    runtime.command(instance.instance_id,Input("wake now"),"input-1")
    done=wait_for(runtime,instance.instance_id,lambda x:x.terminal)
    runtime.shutdown()
    assert done.status == "completed" and policy.calls == 2
    assert "wait_scheduled" in [event.event_type for event in done.events]


def test_pause_and_stop_cancel_a_waiting_timer(tmp_path: Path):
    class AlwaysWait:
        def __init__(self): self.calls=0
        def decide(self, context): self.calls += 1; return Decision(wait_seconds=30)
    policy=AlwaysWait(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=policy, execution_owner=Owner(), verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1")
    wait_for(runtime,instance.instance_id,lambda x:x.status == "waiting")
    paused=runtime.command(instance.instance_id,Pause(),"pause-1")
    time.sleep(0.25)
    assert paused.status == "paused" and policy.calls == 1
    stopped=runtime.command(instance.instance_id,Stop(),"stop-1")
    runtime.shutdown()
    assert stopped.status == "stopped" and policy.calls == 1


def test_pause_and_stop_have_no_late_dispatch(tmp_path: Path):
    entered=threading.Event(); release=threading.Event()
    class Policy:
        def decide(self, context): entered.set(); assert release.wait(1); return Decision(Intent("must-not-send"))
    owner=Owner(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=Policy(), execution_owner=owner, verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1"); assert entered.wait(1)
    runtime.command(instance.instance_id,Pause(),"pause-1"); release.set()
    paused=wait_for(runtime,instance.instance_id,lambda x:x.status == "paused")
    assert owner.dispatches == []
    runtime.command(instance.instance_id,Stop(),"stop-1")
    stopped=wait_for(runtime,instance.instance_id,lambda x:x.status == "stopped")
    runtime.shutdown()
    assert paused.status == "paused" and stopped.terminal and owner.dispatches == []


def test_stop_after_dispatch_preserves_evidence_but_never_completes(tmp_path: Path):
    entered=threading.Event(); release=threading.Event()
    class BlockingVerifier:
        def verify(self, context):
            entered.set(); assert release.wait(1)
            return Outcome("confirmed_success", "verified before stop settled")
    owner=Owner(); runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=OneActionPolicy(), execution_owner=owner, verifier=BlockingVerifier())
    instance=runtime.start("general-v1","start-1")
    assert entered.wait(1)
    stopping=runtime.command(instance.instance_id,Stop(),"stop-1")
    release.set()
    stopped=wait_for(runtime,instance.instance_id,lambda x:x.status == "stopped")
    runtime.shutdown()
    assert stopping.status == "stopping" and stopped.memory_version == 0
    assert len(owner.dispatches) == 1 and stopped.outcomes[-1].status == "confirmed_success"


def test_recovery_inspects_owner_and_never_redispatches_dispatching_intent(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,Intent("do-not-replay"))
    store.mark_reserved(instance.instance_id,"intent-1","owner-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    class ReconcilingOwner(Owner):
        def __init__(self): super().__init__(); self.reconciliations=[]
        def reconcile(self, instance, runtime_intent):
            self.reconciliations.append(runtime_intent.intent_id)
            return ExecutionReconciliation(Outcome("confirmed_success","owner ledger confirmed"),"owner-evidence",ExecutionReceipt("owner-1",True,"confirmed"))
    owner=ReconcilingOwner(); runtime=ApplicationRuntime(database, profile="general-v1", observation_port=Observations(), policy=OneActionPolicy(), execution_owner=owner, verifier=SuccessVerifier())
    recovered=wait_for(runtime,instance.instance_id,lambda x:x.terminal); runtime.shutdown()
    assert recovered.status == "completed" and recovered.degraded is True
    assert owner.reservations == [] and owner.dispatches == []
    assert owner.reconciliations == ["intent-1"]
    assert "recovery_no_replay" in [event.event_type for event in recovered.events]


def test_recovery_reconciles_unfinished_dispatch_before_honoring_stop(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,Intent("do-not-replay"))
    store.mark_reserved(instance.instance_id,"intent-1","owner-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    stopping,_=store.accept_command(instance.instance_id,"Stop",None,"stop-1","digest-stop")
    assert stopping.status == "stopping"
    class ReconcilingOwner(Owner):
        def reconcile(self, instance, runtime_intent):
            return ExecutionReconciliation(Outcome("confirmed_success","owner ledger confirmed",terminal=False),"owner-evidence",ExecutionReceipt("owner-1",True,"confirmed"))
    owner=ReconcilingOwner(); runtime=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=OneActionPolicy(),execution_owner=owner,verifier=SuccessVerifier())
    stopped=wait_for(runtime,instance.instance_id,lambda x:x.status == "stopped")
    runtime.shutdown()
    assert stopped.outcomes[-1].status == "confirmed_success"
    assert owner.dispatches == []


def test_recovery_reconciles_unfinished_dispatch_while_instance_is_paused(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    intent=Intent("already-in-flight")
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,intent)
    assert store.mark_reserved(instance.instance_id,"intent-1","owner-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    paused,_=store.accept_command(instance.instance_id,"Pause",None,"pause-1","digest-pause")
    assert paused.status == "paused" and paused.wake_at is None
    store.release(instance.instance_id,"worker")
    class ReconcilingOwner(Owner):
        def __init__(self): super().__init__(); self.calls=0
        def reconcile(self, state, runtime_intent):
            self.calls += 1
            return ExecutionReconciliation(
                Outcome("confirmed_success","owner confirmed",terminal=False),
                "owner-confirmed",
                ExecutionReceipt("owner-1",True,"confirmed"),
            )
    owner=ReconcilingOwner(); runtime=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=OneActionPolicy(),execution_owner=owner,verifier=SuccessVerifier())
    settled=wait_for(runtime,instance.instance_id,lambda x:x.status == "paused" and bool(x.outcomes)); runtime.shutdown()
    assert owner.calls == 1 and owner.reservations == [] and owner.dispatches == []
    assert settled.outcomes[-1].status == "confirmed_success"


def test_recovery_reconciles_intent_if_crash_followed_owner_inspect_but_preceded_outcome(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    intent=Intent("already-sent")
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,intent)
    assert store.mark_reserved(instance.instance_id,"intent-1","reservation-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    store.mark_reconciled(instance.instance_id,"intent-1",ExecutionReceipt("owner-proof",True))
    store.release(instance.instance_id,"worker")
    class PolicyMustNotRun:
        def decide(self, context): raise AssertionError("open reconciled cycle must converge first")
    class ReconcilingOwner(Owner):
        def __init__(self): super().__init__(); self.reconciliations=[]
        def reconcile(self, instance, runtime_intent):
            self.reconciliations.append(runtime_intent.intent_id)
            return ExecutionReconciliation(Outcome("confirmed_success","owner reconfirmed"),"owner-evidence")
    owner=ReconcilingOwner(); runtime=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=PolicyMustNotRun(),execution_owner=owner,verifier=SuccessVerifier())
    recovered=wait_for(runtime,instance.instance_id,lambda x:x.terminal); runtime.shutdown()
    assert recovered.status == "completed"
    assert owner.reconciliations == ["intent-1"] and owner.dispatches == []


def test_reconciliation_projection_failure_does_not_repeat_owner_inspection(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    intent=Intent("already-sent")
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,intent)
    assert store.mark_reserved(instance.instance_id,"intent-1","reservation-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    store.release(instance.instance_id,"worker")
    class ReconcilingOwner(Owner):
        def __init__(self): super().__init__(); self.calls=0
        def reconcile(self, instance, runtime_intent):
            self.calls += 1
            return ExecutionReconciliation(Outcome("confirmed_success","owner confirmed"),"owner-evidence")
    class FailsTwiceProjection:
        def __init__(self): self.outcome_calls=0
        def project_input(self,value): return value
        def project_observation(self,value): return value
        def project_intent(self,value): return value
        def project_receipt(self,value): return value
        def project_outcome(self,value):
            self.outcome_calls += 1
            if self.outcome_calls <= 2: raise RuntimeError("sensitive projection failure")
            return value
        def project_memory_candidate(self,value): return value
        def project_memory_content(self,scope,value): return value
        def project_detail(self,value): return value
    owner=ReconcilingOwner(); runtime=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=OneActionPolicy(),execution_owner=owner,verifier=SuccessVerifier(),persistence_projection=FailsTwiceProjection())
    failed=wait_for(runtime,instance.instance_id,lambda x:x.terminal); runtime.shutdown()
    assert failed.status == "failed" and failed.error_code == "reconciliation_failed"
    assert owner.calls == 1 and owner.dispatches == []


def test_active_dispatch_reconciliation_polls_owner_only_then_resumes_long_lived_policy(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    intent=Intent("already-in-flight")
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,intent)
    assert store.mark_reserved(instance.instance_id,"intent-1","owner-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    store.release(instance.instance_id,"worker")
    class ActiveThenConfirmedOwner(Owner):
        def __init__(self): super().__init__(); self.reconcile_calls=0
        def reconcile(self, instance, runtime_intent):
            self.reconcile_calls += 1
            if self.reconcile_calls == 1:
                return ExecutionReconciliation(
                    Outcome("unconfirmed","owner action in flight",terminal=False),
                    "owner-active",
                    ExecutionReceipt("owner-1",False,"active_dispatch"),
                    retry_after_seconds=0.2,
                )
            return ExecutionReconciliation(
                Outcome("confirmed_success","owner confirmed",terminal=False),
                "owner-confirmed",
                ExecutionReceipt("owner-1",True,"confirmed"),
            )
    class WaitAfterSettlement:
        def __init__(self): self.calls=0
        def decide(self, context): self.calls += 1; return Decision(wait_seconds=30)
    owner=ActiveThenConfirmedOwner(); policy=WaitAfterSettlement()
    runtime=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=policy,execution_owner=owner,verifier=SuccessVerifier())
    settled=wait_for(runtime,instance.instance_id,lambda x:x.status == "waiting" and len(x.outcomes) == 1)
    runtime.command(instance.instance_id,Stop(),"stop-after-settlement"); runtime.shutdown()
    assert owner.reconcile_calls == 2 and owner.reservations == [] and owner.dispatches == []
    assert policy.calls == 1 and settled.outcomes[0].status == "confirmed_success"
    assert [event.event_type for event in settled.events].count("reconciliation_wait_scheduled") == 1


def test_definite_not_dispatched_reconciliation_reobserves_without_replay(
    tmp_path: Path,
):
    database = tmp_path / "runtime.db"
    store = _SQLiteApplicationStore(database)
    instance, _ = store.accept_start(
        "instance-1", "general-v1", None, None, "start-1", "digest-start"
    )
    assert store.claim(instance.instance_id, "worker")
    cycle, revision = store.begin_cycle(instance.instance_id, "worker") or (None, None)
    assert cycle is not None
    assert store.persist_intent(
        "intent-1",
        instance.instance_id,
        cycle,
        revision,
        Intent("definitely-not-sent"),
    )
    assert store.mark_reserved(instance.instance_id, "intent-1", "owner-1")
    assert store.mark_dispatching(instance.instance_id, "intent-1", revision)
    store.release(instance.instance_id, "worker")

    class DefiniteNoDispatchOwner(Owner):
        def __init__(self):
            super().__init__()
            self.reconcile_calls = 0

        def reconcile(self, state, runtime_intent):
            self.reconcile_calls += 1
            return ExecutionReconciliation(
                Outcome(
                    "confirmed_failure",
                    "owner proved no physical dispatch",
                    terminal=False,
                ),
                "owner-definite-no-dispatch",
                ExecutionReceipt("owner-1", False, "preclick_rejected"),
            )

    class WaitAfterReobserve:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            return Decision(wait_seconds=30)

    owner = DefiniteNoDispatchOwner()
    policy = WaitAfterReobserve()
    runtime = ApplicationRuntime(
        database,
        profile="general-v1",
        observation_port=Observations(),
        policy=policy,
        execution_owner=owner,
        verifier=SuccessVerifier(),
    )
    settled = wait_for(
        runtime,
        instance.instance_id,
        lambda value: value.status == "waiting" and len(value.outcomes) == 1,
    )
    runtime.command(instance.instance_id, Stop(), "cleanup")
    runtime.shutdown()

    assert owner.reconcile_calls == 1
    assert owner.reservations == [] and owner.dispatches == []
    assert policy.calls == 1
    assert settled.outcomes[-1].status == "confirmed_failure"
    assert settled.outcomes[-1].terminal is False


@pytest.mark.parametrize("status", ["unconfirmed", "uncertain"])
def test_nondefinitive_reconciliation_never_replans_without_a_retry_directive(
    tmp_path: Path,
    status: str,
):
    database = tmp_path / f"{status}.db"
    store = _SQLiteApplicationStore(database)
    instance, _ = store.accept_start(
        "instance-1", "general-v1", None, None, "start-1", "digest-start"
    )
    assert store.claim(instance.instance_id, "worker")
    cycle, revision = store.begin_cycle(instance.instance_id, "worker") or (None, None)
    assert cycle is not None
    assert store.persist_intent(
        "intent-1", instance.instance_id, cycle, revision, Intent("maybe-sent")
    )
    assert store.mark_reserved(instance.instance_id, "intent-1", "owner-1")
    assert store.mark_dispatching(instance.instance_id, "intent-1", revision)
    store.release(instance.instance_id, "worker")

    class NondefinitiveOwner(Owner):
        def reconcile(self, state, runtime_intent):
            return ExecutionReconciliation(
                Outcome(status, "owner could not prove no dispatch", terminal=False),
                f"owner-{status}",
                ExecutionReceipt("owner-1", False, status),
            )

    class MustNotReplan:
        def decide(self, context):
            raise AssertionError("nondefinitive physical action must not be replanned")

    owner = NondefinitiveOwner()
    runtime = ApplicationRuntime(
        database,
        profile="general-v1",
        observation_port=Observations(),
        policy=MustNotReplan(),
        execution_owner=owner,
        verifier=SuccessVerifier(),
    )
    failed = wait_for(runtime, instance.instance_id, lambda value: value.terminal)
    runtime.shutdown()

    assert failed.status == "failed"
    assert failed.error_code == "recovery_no_replay"
    assert owner.reservations == [] and owner.dispatches == []


@pytest.mark.parametrize(
    "command",
    [Input("late instruction"), Pause(), Stop()],
)
def test_late_command_cannot_override_uncertain_owner_settlement(
    tmp_path: Path,
    command,
):
    database = tmp_path / f"{command.tag}.db"
    store = _SQLiteApplicationStore(database)
    instance, _ = store.accept_start(
        "instance-1", "general-v1", None, None, "start-1", "digest-start"
    )
    assert store.claim(instance.instance_id, "worker")
    cycle, revision = store.begin_cycle(instance.instance_id, "worker") or (None, None)
    assert cycle is not None
    assert store.persist_intent(
        "intent-1", instance.instance_id, cycle, revision, Intent("maybe-sent")
    )
    assert store.mark_reserved(instance.instance_id, "intent-1", "owner-1")
    assert store.mark_dispatching(instance.instance_id, "intent-1", revision)
    store.release(instance.instance_id, "worker")
    entered = threading.Event()
    release = threading.Event()

    class BlockingUncertainOwner(Owner):
        def reconcile(self, state, runtime_intent):
            entered.set()
            assert release.wait(1)
            return ExecutionReconciliation(
                Outcome("uncertain", "owner cannot prove delivery"),
                "owner-uncertain",
                ExecutionReceipt("owner-1", False, "uncertain"),
            )

    class MustNotReplan:
        def decide(self, context):
            raise AssertionError("late command cannot authorize replay")

    owner = BlockingUncertainOwner()
    runtime = ApplicationRuntime(
        database,
        profile="general-v1",
        observation_port=Observations(),
        policy=MustNotReplan(),
        execution_owner=owner,
        verifier=SuccessVerifier(),
    )
    assert entered.wait(1)
    runtime.command(instance.instance_id, command, f"late-{command.tag}")
    release.set()
    failed = wait_for(runtime, instance.instance_id, lambda value: value.terminal)
    runtime.shutdown()

    assert failed.status == "failed"
    assert failed.error_code == "recovery_no_replay"
    assert failed.outcomes[-1].status == "uncertain"
    assert owner.reservations == [] and owner.dispatches == []


def test_reconciliation_persists_effective_intent_hard_risk(tmp_path: Path):
    database = tmp_path / "runtime.db"
    store = _SQLiteApplicationStore(database)
    instance, _ = store.accept_start(
        "instance-1", "general-v1", None, None, "start-1", "digest-start"
    )
    assert store.claim(instance.instance_id, "worker")
    cycle, revision = store.begin_cycle(instance.instance_id, "worker") or (None, None)
    assert cycle is not None
    assert store.persist_intent(
        "intent-1",
        instance.instance_id,
        cycle,
        revision,
        Intent("hard-action", hard_risk=True),
    )
    assert store.mark_reserved(instance.instance_id, "intent-1", "owner-1")
    assert store.mark_dispatching(instance.instance_id, "intent-1", revision)
    store.release(instance.instance_id, "worker")

    class UncertainOwner(Owner):
        def reconcile(self, state, runtime_intent):
            return ExecutionReconciliation(
                Outcome("uncertain", "owner cannot prove delivery"),
                "owner-uncertain",
                ExecutionReceipt("owner-1", False, "uncertain"),
            )

    runtime = ApplicationRuntime(
        database,
        profile="general-v1",
        observation_port=Observations(),
        policy=OneActionPolicy(),
        execution_owner=UncertainOwner(),
        verifier=SuccessVerifier(),
    )
    failed = wait_for(runtime, instance.instance_id, lambda value: value.terminal)
    runtime.shutdown()

    assert failed.status == "failed" and failed.hard_risk is True
    assert failed.error_code == "recovery_hard_risk"
    assert failed.outcomes[-1].hard_risk is True


def test_retryable_pre_intent_failure_persists_wait_then_recovers(
    tmp_path: Path,
):
    class TransientObservation:
        def __init__(self):
            self.calls = 0

        def observe(self, instance):
            self.calls += 1
            if self.calls == 1:
                raise RetryableApplicationError(0.2)
            return Observation(f"observation-{self.calls}")

    class WaitAfterRecovery:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            return Decision(wait_seconds=30)

    observations = TransientObservation()
    policy = WaitAfterRecovery()
    runtime = ApplicationRuntime(
        tmp_path / "runtime.db",
        profile="general-v1",
        observation_port=observations,
        policy=policy,
        execution_owner=Owner(),
        verifier=SuccessVerifier(),
    )
    instance = runtime.start("general-v1", "start-1")
    recovered = wait_for(
        runtime,
        instance.instance_id,
        lambda value: (
            value.status == "waiting"
            and observations.calls >= 2
            and policy.calls == 1
        ),
    )
    runtime.command(instance.instance_id, Stop(), "cleanup")
    runtime.shutdown()

    assert recovered.degraded is True
    assert not recovered.terminal
    assert any(
        event.event_type == "runtime_retry_scheduled" for event in recovered.events
    )


@pytest.mark.parametrize(
    ("command", "expected_status"),
    [
        (Input("new instruction"), "waiting"),
        (Pause(), "paused"),
        (Stop(), "stopped"),
    ],
)
def test_policy_cancellation_tracks_input_pause_and_stop_without_dispatch(
    tmp_path: Path,
    command,
    expected_status: str,
):
    entered = threading.Event()
    cancelled = threading.Event()

    class CooperativePolicy:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            if self.calls > 1:
                return Decision(wait_seconds=30)
            assert context.is_cancelled() is False
            entered.set()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not context.is_cancelled():
                time.sleep(0.005)
            assert context.is_cancelled() is True
            cancelled.set()
            raise RetryableApplicationError(0.2)

    owner = Owner()
    policy = CooperativePolicy()
    runtime = ApplicationRuntime(
        tmp_path / "runtime.db",
        profile="general-v1",
        observation_port=Observations(),
        policy=policy,
        execution_owner=owner,
        verifier=SuccessVerifier(),
    )
    instance = runtime.start("general-v1", "start-1")
    assert entered.wait(1)
    runtime.command(instance.instance_id, command, "interrupt-1")
    assert cancelled.wait(1)
    settled = wait_for(
        runtime,
        instance.instance_id,
        lambda value: value.status == expected_status,
    )
    if not settled.terminal:
        runtime.command(instance.instance_id, Stop(), "cleanup")
    runtime.shutdown()

    assert owner.reservations == [] and owner.dispatches == []
    assert all(event.event_type != "runtime_retry_scheduled" for event in settled.events)


def test_policy_cancellation_tracks_runtime_shutdown(tmp_path: Path):
    entered = threading.Event()
    cancelled = threading.Event()

    class CooperativePolicy:
        def decide(self, context):
            entered.set()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not context.is_cancelled():
                time.sleep(0.005)
            if context.is_cancelled():
                cancelled.set()
                raise RetryableApplicationError(0.2)
            raise AssertionError("shutdown did not cancel policy generation")

    runtime = ApplicationRuntime(
        tmp_path / "runtime.db",
        profile="general-v1",
        observation_port=Observations(),
        policy=CooperativePolicy(),
        execution_owner=Owner(),
        verifier=SuccessVerifier(),
    )
    runtime.start("general-v1", "start-1")
    assert entered.wait(1)
    runtime.shutdown(timeout=1.0)

    assert cancelled.is_set()


def test_stale_timer_callback_cannot_remove_replacement_timer(tmp_path: Path):
    class AlwaysWait:
        def decide(self, context):
            return Decision(wait_seconds=30)

    runtime = ApplicationRuntime(
        tmp_path / "runtime.db",
        profile="general-v1",
        observation_port=Observations(),
        policy=AlwaysWait(),
        execution_owner=Owner(),
        verifier=SuccessVerifier(),
    )
    instance = runtime.start("general-v1", "start-1")
    wait_for(runtime, instance.instance_id, lambda value: value.status == "waiting")
    deadline = time.monotonic() + 1.0
    old_entry = None
    while time.monotonic() < deadline:
        with runtime._timer_lock:
            old_entry = runtime._timers.get(instance.instance_id)
        if old_entry is not None:
            break
        time.sleep(0.005)
    assert old_entry is not None
    old_token, old_wake_at, _old_timer = old_entry

    runtime.command(instance.instance_id, Input("wake now"), "input-1")
    deadline = time.monotonic() + 1.0
    replacement = None
    while time.monotonic() < deadline:
        with runtime._timer_lock:
            current = runtime._timers.get(instance.instance_id)
        if current is not None and current[0] != old_token:
            replacement = current
            break
        time.sleep(0.005)
    assert replacement is not None

    runtime._timer_elapsed(instance.instance_id, old_token, old_wake_at)
    with runtime._timer_lock:
        current = runtime._timers.get(instance.instance_id)
    assert current is not None and current[0] == replacement[0]
    assert runtime.inspect(instance.instance_id).status == "waiting"
    runtime.command(instance.instance_id, Stop(), "cleanup")
    runtime.shutdown()


def test_timer_registration_is_atomic_with_interrupting_command(
    tmp_path: Path,
    monkeypatch,
):
    class AlwaysWait:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            return Decision(wait_seconds=30)

    policy = AlwaysWait()
    runtime = ApplicationRuntime(
        tmp_path / "runtime.db",
        profile="general-v1",
        observation_port=Observations(),
        policy=policy,
        execution_owner=Owner(),
        verifier=SuccessVerifier(),
    )
    real_inspect = runtime._store.inspect
    schedule_entered = threading.Event()
    release_schedule = threading.Event()
    blocked_once = False

    def blocking_schedule_inspect(instance_id):
        nonlocal blocked_once
        state = real_inspect(instance_id)
        if (
            not blocked_once
            and threading.current_thread().name == "application-runtime-coordinator"
            and state.status == "waiting"
        ):
            blocked_once = True
            schedule_entered.set()
            assert release_schedule.wait(1)
        return state

    monkeypatch.setattr(runtime._store, "inspect", blocking_schedule_inspect)
    instance = runtime.start("general-v1", "start-1")
    assert schedule_entered.wait(1)
    command_returned = threading.Event()

    def interrupt_wait():
        runtime.command(instance.instance_id, Input("wake now"), "input-1")
        command_returned.set()

    command_thread = threading.Thread(target=interrupt_wait)
    command_thread.start()
    assert not command_returned.wait(0.08)
    release_schedule.set()
    assert command_returned.wait(1)
    command_thread.join()
    settled = wait_for(
        runtime,
        instance.instance_id,
        lambda value: value.status == "waiting" and policy.calls >= 2,
    )
    runtime.command(instance.instance_id, Stop(), "cleanup")
    runtime.shutdown()

    assert settled.revision == 1


def test_second_runtime_cannot_recover_live_database_owner(tmp_path: Path):
    database = tmp_path / "runtime.db"
    entered = threading.Event()
    release = threading.Event()

    class BlockingPolicy:
        def decide(self, context):
            entered.set()
            assert release.wait(1)
            return Decision(wait_seconds=30)

    first = ApplicationRuntime(
        database,
        profile="general-v1",
        observation_port=Observations(),
        policy=BlockingPolicy(),
        execution_owner=Owner(),
        verifier=SuccessVerifier(),
    )
    instance = first.start("general-v1", "start-1")
    assert entered.wait(1)
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT status,worker_token FROM application_instances WHERE instance_id=?",
            (instance.instance_id,),
        ).fetchone()
    assert before is not None and before[0] == "running" and before[1]

    with pytest.raises(RuntimeError, match="database is already active"):
        ApplicationRuntime(
            database,
            profile="general-v1",
            observation_port=Observations(),
            policy=OneActionPolicy(),
            execution_owner=Owner(),
            verifier=SuccessVerifier(),
        )
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT status,worker_token FROM application_instances WHERE instance_id=?",
            (instance.instance_id,),
        ).fetchone()
    assert after == before

    release.set()
    wait_for(first, instance.instance_id, lambda value: value.status == "waiting")
    first.shutdown()
    second = ApplicationRuntime(
        database,
        profile="general-v1",
        observation_port=Observations(),
        policy=OneActionPolicy(),
        execution_owner=Owner(),
        verifier=SuccessVerifier(),
    )
    second.command(instance.instance_id, Stop(), "cleanup")
    second.shutdown()


def test_active_dispatch_wait_survives_restart_and_recovers_by_inspection_only(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    instance,_=store.accept_start("instance-1","general-v1",None,None,"start-1","digest-start")
    assert store.claim(instance.instance_id,"worker")
    cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
    assert cycle is not None
    intent=Intent("already-in-flight")
    assert store.persist_intent("intent-1",instance.instance_id,cycle,revision,intent)
    assert store.mark_reserved(instance.instance_id,"intent-1","owner-1")
    assert store.mark_dispatching(instance.instance_id,"intent-1",revision)
    store.release(instance.instance_id,"worker")
    class ActiveOwner(Owner):
        def reconcile(self, state, runtime_intent):
            return ExecutionReconciliation(
                Outcome("unconfirmed","owner action in flight",terminal=False),
                "owner-active",
                ExecutionReceipt("owner-1",False,"active_dispatch"),
                retry_after_seconds=0.2,
            )
    class ConfirmedOwner(Owner):
        def __init__(self): super().__init__(); self.calls=0
        def reconcile(self, state, runtime_intent):
            self.calls += 1
            return ExecutionReconciliation(
                Outcome("confirmed_success","owner confirmed",terminal=False),
                "owner-confirmed",
                ExecutionReceipt("owner-1",True,"confirmed"),
            )
    class WaitPolicy:
        def decide(self, context): return Decision(wait_seconds=30)
    first=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=WaitPolicy(),execution_owner=ActiveOwner(),verifier=SuccessVerifier())
    wait_for(first,instance.instance_id,lambda x:x.status == "waiting" and x.wake_at is not None)
    first.shutdown(); time.sleep(0.22)
    owner=ConfirmedOwner(); second=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=WaitPolicy(),execution_owner=owner,verifier=SuccessVerifier())
    settled=wait_for(second,instance.instance_id,lambda x:x.status == "waiting" and bool(x.outcomes))
    second.command(instance.instance_id,Stop(),"cleanup"); second.shutdown()
    assert owner.calls == 1 and owner.reservations == [] and owner.dispatches == []
    assert settled.outcomes[-1].status == "confirmed_success"


def test_input_pause_and_stop_wake_in_flight_reconciliation_without_replay(tmp_path: Path):
    cases=(("input",Input("new instruction"),"waiting"),("pause",Pause(),"paused"),("stop",Stop(),"stopped"))
    for name,command,expected_status in cases:
        database=tmp_path / f"{name}.db"; store=_SQLiteApplicationStore(database)
        instance,_=store.accept_start(f"instance-{name}","general-v1",None,None,f"start-{name}",f"digest-{name}")
        assert store.claim(instance.instance_id,"worker")
        cycle,revision=store.begin_cycle(instance.instance_id,"worker") or (None,None)
        assert cycle is not None
        intent=Intent("already-in-flight")
        assert store.persist_intent(f"intent-{name}",instance.instance_id,cycle,revision,intent)
        assert store.mark_reserved(instance.instance_id,f"intent-{name}",f"owner-{name}")
        assert store.mark_dispatching(instance.instance_id,f"intent-{name}",revision)
        store.release(instance.instance_id,"worker")
        class ActiveThenConfirmedOwner(Owner):
            def __init__(self): super().__init__(); self.calls=0
            def reconcile(self, state, runtime_intent):
                self.calls += 1
                if self.calls == 1:
                    return ExecutionReconciliation(
                        Outcome("unconfirmed","owner action in flight",terminal=False),
                        f"active-{name}",
                        ExecutionReceipt(f"owner-{name}",False,"active_dispatch"),
                        retry_after_seconds=30,
                    )
                return ExecutionReconciliation(
                    Outcome("confirmed_success","owner confirmed",terminal=False),
                    f"confirmed-{name}",
                    ExecutionReceipt(f"owner-{name}",True,"confirmed"),
                )
        class WaitPolicy:
            def decide(self, context): return Decision(wait_seconds=30)
        owner=ActiveThenConfirmedOwner(); runtime=ApplicationRuntime(database,profile="general-v1",observation_port=Observations(),policy=WaitPolicy(),execution_owner=owner,verifier=SuccessVerifier())
        waiting=wait_for(runtime,instance.instance_id,lambda x:x.status == "waiting" and x.wake_at is not None)
        runtime.command(instance.instance_id,command,f"command-{name}")
        settled=wait_for(runtime,instance.instance_id,lambda x:bool(x.outcomes) and x.status == expected_status)
        if not settled.terminal:
            runtime.command(instance.instance_id,Stop(),f"cleanup-{name}")
        runtime.shutdown()
        assert waiting.outcomes == ()
        assert owner.calls == 2 and owner.reservations == [] and owner.dispatches == []
        assert settled.outcomes[-1].status == "confirmed_success"


def test_wait_timer_exception_is_persisted_instead_of_stranding_instance(tmp_path: Path, monkeypatch):
    class WaitPolicy:
        def decide(self, context): return Decision(wait_seconds=30)
    runtime=ApplicationRuntime(tmp_path / "runtime.db",profile="general-v1",observation_port=Observations(),policy=WaitPolicy(),execution_owner=Owner(),verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1")
    waiting=wait_for(runtime,instance.instance_id,lambda x:x.status == "waiting")
    deadline=time.monotonic()+1
    while instance.instance_id not in runtime._timers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.instance_id in runtime._timers
    def fail_wakeup(instance_id, wake_at=None): raise RuntimeError("database wake failed")
    monkeypatch.setattr(runtime._store,"wake_scheduled",fail_wakeup)
    timer_token = runtime._timers[instance.instance_id][0]
    runtime._timer_elapsed(instance.instance_id,timer_token,waiting.wake_at)
    failed=wait_for(runtime,instance.instance_id,lambda x:x.terminal); runtime.shutdown()
    assert failed.status == "failed" and failed.error_code == "wait_wakeup_failed"


def test_policy_exception_becomes_durable_failure_instead_of_silent_running(tmp_path: Path):
    class BrokenPolicy:
        def decide(self, context): raise LookupError("sensitive model response")
    runtime=ApplicationRuntime(tmp_path / "runtime.db", profile="general-v1", observation_port=Observations(), policy=BrokenPolicy(), execution_owner=Owner(), verifier=SuccessVerifier())
    instance=runtime.start("general-v1","start-1")
    failed=wait_for(runtime,instance.instance_id,lambda x:x.terminal)
    runtime.shutdown()
    assert failed.status == "failed" and failed.degraded is True
    assert failed.error_code == "policy_failed"
    assert "sensitive model response" not in repr(failed)
    assert "runtime_error" in [event.event_type for event in failed.events]


def test_nonterminal_verified_action_requeues_and_memory_reward_is_not_assumed(tmp_path: Path):
    candidate=MemoryCandidate("soul-reply-v1",{"strategy":"short"},("delivery-proof",),reward_required=True)
    class OneReplyThenWait:
        def __init__(self): self.calls=0
        def decide(self, context):
            self.calls += 1
            if self.calls == 1: return Decision(Intent("reply"),memory_candidate=candidate)
            return Decision(wait_seconds=30)
    class DeliveryVerifier:
        def verify(self, context): return Outcome("confirmed_success","delivery confirmed",terminal=False)
    class Gate:
        def promote(self, candidate, outcome): raise AssertionError("delivery proof is not learning reward")
    policy=OneReplyThenWait(); database=tmp_path / "runtime.db"
    runtime=ApplicationRuntime(database, profile="soul", memory_scope="soul-reply-v1", observation_port=Observations(), policy=policy, execution_owner=Owner(), verifier=DeliveryVerifier(), memory_gate=Gate())
    instance=runtime.start("soul","start-1")
    waiting=wait_for(runtime,instance.instance_id,lambda x:x.status == "waiting")
    runtime.command(instance.instance_id,Stop(),"stop-1"); runtime.shutdown()
    with sqlite3.connect(database) as conn:
        row=conn.execute("SELECT reward_required,eligible FROM application_memory_candidates").fetchone()
    assert waiting.memory_version == 0 and row == (1,0) and policy.calls == 2


def test_explicit_memory_scope_is_used_for_reads(tmp_path: Path):
    database=tmp_path / "runtime.db"; store=_SQLiteApplicationStore(database)
    store.promote_memory("seed-instance","candidate",MemoryCandidate("soul-reply-v1",{"hint":"old"},("proof",)),Outcome("confirmed_success"),{"hint":"active"})
    class ReadMemoryThenAct(OneActionPolicy):
        def decide(self, context):
            assert context.active_memory == {"hint":"active"}
            return super().decide(context)
    runtime=ApplicationRuntime(database, profile="soul", memory_scope="soul-reply-v1", observation_port=Observations(), policy=ReadMemoryThenAct(), execution_owner=Owner(), verifier=SuccessVerifier())
    instance=runtime.start("soul","start-1")
    wait_for(runtime,instance.instance_id,lambda x:x.terminal); runtime.shutdown()


def test_store_connections_are_closed_after_each_operation(tmp_path: Path, monkeypatch):
    import ai_game_console.application_runtime.store as store_module
    real_connect=store_module.sqlite3.connect; opened=[]
    class TrackedConnection(sqlite3.Connection):
        def close(self): self.was_closed=True; return super().close()
    def tracked_connect(*args,**kwargs):
        kwargs["factory"]=TrackedConnection; conn=real_connect(*args,**kwargs); conn.was_closed=False; opened.append(conn); return conn
    monkeypatch.setattr(store_module.sqlite3,"connect",tracked_connect)
    store=_SQLiteApplicationStore(tmp_path / "runtime.db")
    store.list(10)
    assert opened and all(conn.was_closed for conn in opened)


def test_projection_persists_only_safe_values_but_current_cycle_uses_raw_values(tmp_path: Path):
    database=tmp_path / "runtime.db"; raw_seen=[]
    class SensitiveObservations:
        def __init__(self): self.calls=0
        def observe(self, instance):
            self.calls += 1
            return Observation(f"raw-evidence-{self.calls}","raw-title",data={"transcript":"raw-transcript","screenshot":"raw-screenshot"})
    class SensitivePolicy:
        def decide(self, context):
            raw_seen.append(context.before.data["transcript"])
            return Decision(Intent("reply",{"draft":"raw-draft"}))
    class SensitiveOwner:
        def reserve(self, instance, intent): raw_seen.append(intent.arguments["draft"]); return "owner-ref"
        def dispatch(self, reservation_id, instance, intent): raw_seen.append(intent.arguments["draft"]); return ExecutionReceipt("raw-receipt",True,"raw-receipt-detail")
    class SensitiveVerifier:
        def verify(self, context):
            raw_seen.append(context.after.data["screenshot"])
            return Outcome("confirmed_success","raw-outcome")
    class Projection:
        def project_input(self,value): return "input-ref"
        def project_observation(self,value): return Observation(f"safe-{value.evidence_id[-1]}",fresh=value.fresh,data={"context_ref":"opaque"})
        def project_intent(self,value): return Intent(value.name,{"draft_sha256":"opaque"},value.hard_risk)
        def project_receipt(self,value): return ExecutionReceipt("safe-receipt",value.accepted,"confirmed")
        def project_outcome(self,value): return Outcome(value.status,"safe-outcome",value.hard_risk,value.terminal)
        def project_memory_candidate(self,value): return MemoryCandidate(value.scope,{"trial_ref":"opaque"},("safe",),value.reward_required)
        def project_memory_content(self,scope,value): return {"memory_ref":"opaque"}
        def project_detail(self,value): return "safe-detail"
    runtime=ApplicationRuntime(database,profile="soul",observation_port=SensitiveObservations(),policy=SensitivePolicy(),execution_owner=SensitiveOwner(),verifier=SensitiveVerifier(),persistence_projection=Projection())
    instance=runtime.start("soul","start-1",initial_input="raw-input")
    done=wait_for(runtime,instance.instance_id,lambda x:x.terminal); runtime.shutdown()
    persisted=database.read_bytes()
    assert raw_seen == ["raw-transcript","raw-draft","raw-draft","raw-screenshot"]
    for secret in (b"raw-input",b"raw-title",b"raw-transcript",b"raw-screenshot",b"raw-draft",b"raw-receipt",b"raw-outcome"):
        assert secret not in persisted
    assert done.initial_input == "input-ref"
    assert done.intents[-1].intent.arguments == {"draft_sha256":"opaque"}
    assert done.outcomes[-1].evidence == "safe-outcome"


def test_v1_database_is_migrated_in_place_for_wait_reward_and_terminal_fields(tmp_path: Path):
    database=tmp_path / "runtime.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
        CREATE TABLE application_runtime_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL);
        INSERT INTO application_runtime_schema VALUES(1,1);
        CREATE TABLE application_instances(
          instance_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, target_id TEXT,
          initial_input TEXT, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
          degraded INTEGER NOT NULL DEFAULT 0, hard_risk INTEGER NOT NULL DEFAULT 0,
          detail TEXT, error_code TEXT, memory_version INTEGER NOT NULL DEFAULT 0,
          worker_token TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT);
        CREATE TABLE application_outcomes(
          instance_id TEXT NOT NULL, cycle INTEGER NOT NULL, status TEXT NOT NULL,
          evidence TEXT NOT NULL, hard_risk INTEGER NOT NULL, after_evidence_id TEXT,
          created_at TEXT NOT NULL, PRIMARY KEY(instance_id,cycle));
        CREATE TABLE application_memory_candidates(
          candidate_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, scope TEXT NOT NULL,
          content_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
          eligible INTEGER NOT NULL, created_at TEXT NOT NULL);
        """)
    _SQLiteApplicationStore(database)
    with sqlite3.connect(database) as conn:
        version=conn.execute("SELECT version FROM application_runtime_schema").fetchone()[0]
        instance_columns={row[1] for row in conn.execute("PRAGMA table_info(application_instances)")}
        outcome_columns={row[1] for row in conn.execute("PRAGMA table_info(application_outcomes)")}
        candidate_columns={row[1] for row in conn.execute("PRAGMA table_info(application_memory_candidates)")}
    assert version == 2
    assert "wake_at" in instance_columns
    assert "terminal" in outcome_columns
    assert "reward_required" in candidate_columns
