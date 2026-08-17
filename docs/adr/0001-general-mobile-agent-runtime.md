# ADR 0001: Keep AI-GAME general through ApplicationRuntime Profiles

## Status

Accepted on 2026-08-10; updated to reflect the landed ApplicationRuntime and
`soul-reply-v1` implementation.

## Context

AI-GAME already has a local GUI-Owl screenshot-to-action loop, Android
execution, fresh post-action screenshots, MobileTask planning and verification,
chat orchestration, game-learning verification, and policy memory. Installing
Mobile-Agent-v3.5 wholesale would duplicate these working implementations and
add another enclosing lifecycle.

Applications such as Soul need a reusable long-running cycle—observation,
policy, durable intent, physical execution, verification, waiting, recovery and
learning—but their physical truth and risk rules differ. Turning AI-GAME into a
Soul-only console would discard the intended game and general-app scope; making
every domain a shallow universal Run would erase their different completion and
recovery semantics.

Soul also has a mature external owner. `F:\dating-copilot` already owns the
device, exact conversation identity, send ledger and echo/reconciliation proof.
Duplicating ADB or that ledger inside AI-GAME would introduce races and false
replay safety.

## Decision

AI-GAME remains a general mobile intelligent-agent platform. The deep
`ApplicationRuntime` Module owns durable `ApplicationInstance` lifecycle and a
serialized application cycle behind this caller Interface:

```text
start(profile_id, client_request_id, target_id=None, initial_input=None)
command(instance_id, Input|Pause|Resume|Stop, client_request_id)
inspect(instance_id)
list(limit=100)
shutdown(timeout=5)
```

Each Application Profile supplies internal `ObservationPort`, `Policy`,
`ExecutionOwner`, `Verifier`, optional `MemoryGate`, and persistence-projection
Adapters. The runtime persists an intent before execution, shares one final
revision/dispatch fence with commands, and sends unfinished intents through
inspect-only reconciliation. It never automatically re-dispatches an uncertain
physical intent.

The public HTTP Adapter is
`/api/v1/application-instances`: create, list, inspect, and
`/{instance_id}/commands`. Content-bound `client_request_id` values make exact
retries idempotent and conflicting reuse explicit. The projection exposes
lifecycle/risk/count/timestamp facts, not application message bodies, draft
text, screenshots, raw owner receipts or evidence bodies.

Soul is one Profile, `soul-reply-v1`. AI-GAME owns its long-lived application
instance, local visual-fact extraction, transient cloud reply policy, revision
fence, owner-proof verification and delayed reply-strategy learning.
`dating-copilot` remains the sole Soul device and physical-ledger owner. The
production Adapter uses only its loopback v1 observation, reserve, dispatch and
inspect Interface plus its managed scheduler GET/PUT seam; AI-GAME never imports
its Python code, opens its database, controls its ADB session, or starts/stops
the dating-copilot process.

The managed scheduler and Application reply loop remain separate deep
responsibilities. dating-copilot keeps all-day matching, immediate match
openers, bounded unread discovery, and trustworthy-zero-quota Planet outreach;
it does not generate ordinary replies. Application Start/Resume targets the
matcher as running, Pause targets paused, Input leaves it unchanged, and Stop
targets stopped only after the Application core reaches durable `stopped`.
Failed/completed reply instances and AI-GAME process shutdown do not implicitly
stop the matcher.

A content-free `soul-scheduler-lifecycle.db` singleton stores requested and
actionable desired state, source instance, hash transition ref, monotonic
generation and timestamp. It aggregates every nonterminal Soul instance:
running demand wins over paused demand, while `stopping` contributes its
pre-Stop target. With none, the newest explicit lifecycle evidence applies, but
only a core-settled Stop selects stopped; failed/completed instances preserve
their latest explicit target rather than reviving an older Stop. One
interruptible GET-before-PUT reconciler repairs owner reverse restarts and
retries temporary unavailability without creating another instance. Cold
paused recovery need not briefly run. With reply dependencies unavailable, the
dependency-free core may settle only a genuinely idle Stop; worker-owned or
unfinished intents remain untouched.

Soul screenshots are captured by the owner and consumed only by AI-GAME's
loopback local vision Adapter. Screenshot bytes are removed before policy and
persistence handoff. The cloud provider receives transcript text and structured
local visual facts, not raw screenshots. Before dispatch, dating-copilot must
recheck the same `conversation_revision` and immutable draft.

A confirmed send is delivery proof, not a strategy reward. Soul reply learning
stores hash-bound trial lineage and changes strategy only after admissible later
interaction evidence. The Profile's immediate `MemoryGate` therefore refuses to
promote a successful current send. Once the owner has reserved or delivered,
failure to bind or write send proof in the local learning database cannot
rewrite that owner fact or create another reserve/send path.

Owner `active_dispatch` recovery is GET/inspect-only. Direct
`legacy_scheduler_active` and `soul_execution_runtime_unavailable` responses
are definite-not-sent and, after `terminal_no_replay` inspection, allow a fresh
observation/re-plan without replaying the old intent or draft.

The old SoulIntegration GET workspace and conversation routes remain read-only
diagnostics. Its two legacy POST command routes return HTTP 410 and its command
receipt ledger is no longer the main runtime path.

MobileTask, Chat, Game Learning and Target discovery keep their own deep Modules
and truth semantics. The primary browser navigation is limited to one-sentence
start, Soul, Device, and Settings. Mobile-Agent remains a design and
implementation reference, not an installed parent runtime.

## Alternatives considered

### Install Mobile-Agent-v3.5 as the parent framework

Rejected. It duplicates the execution core, increases model/process complexity,
and makes AI-GAME depend on another orchestration lifecycle before the needed
domain semantics are stable.

### Make every domain use one universal Run record

Rejected. MobileTask plans, Chat Turns, LearningJobs, and physical application
intents have different evidence, completion, cancellation and recovery rules.
A shallow common status would recreate false completion states.

### Let AI-GAME operate Soul through generic ADB

Rejected. The ApplicationRuntime dispatch fence is not an inter-process device
lock. Direct AI-GAME ADB would race the dating-copilot controller and bypass its
physical send/acquisition ledger and echo proof.

### Keep the old Soul command receipt bridge as the scheduler

Rejected. It reports command acceptance rather than a complete
observation/policy/physical-proof cycle and cannot support same-cycle revision
fencing, delayed reply-learning lineage, monotonic lifecycle ordering, or
GET-before-PUT convergence. The replacement is the narrow managed scheduler
owner seam, not the old generic command proxy.

## Consequences

- A new application can reuse one durable cycle without copying Soul policy.
- Callers see one small instance Interface while Profile-specific complexity
  remains local to its Adapters.
- Owner commands cannot land between the last revision check and a physical
  owner result.
- An ambiguous or interrupted physical operation is inspected, never replayed.
- Soul can use local vision and cloud language generation without uploading raw
  screenshots or moving device ownership into AI-GAME.
- Matcher desired state survives either process restarting, while reply/model
  dependency failure does not silently stop all-day matching.
- Explicit Application Stop and process shutdown are distinct: only the former,
  after core settlement, can target the external matcher as stopped.
- Delivery, later engagement, strategy change, code/tests, and live acceptance
  remain separate facts.
- The supplied completed 率土之滨 Task
  `daac81a7-1af9-47e3-9566-66e73509a0fd` (23 ActionAttempts,
  `auto:stzb/tutorial/v1`) is one task-specific live result, not general game
  competence. The new Soul reply chain still requires this round's live
  acceptance.
