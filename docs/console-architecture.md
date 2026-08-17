# General mobile-agent console architecture

## Product boundary

AI-GAME is a Windows-native, loopback-only control plane and browser UI for a
general local phone Operator. The reusable application-cycle platform is the
deep `ApplicationRuntime` Module; application-specific behavior arrives through
Profiles and their Adapters. `soul-reply-v1` is one such Profile, not the
product boundary. The one-sentence `MobileTask` remains the primary work surface
for arbitrary long-horizon Android goals.

Chat and bounded game learning remain separate Modules with deliberately
different domain and recovery semantics. Chat has two modes:

- `local_chat`: persistent text dialogue through the local GUI-Owl endpoint;
  it never loads a target, captures a screenshot, calls a cloud provider, or
  sends ADB input.
- `cloud_execute`: an independently configured OpenAI-compatible cloud model
  produces the user-facing reply and a structured high-level execution goal. A
  local GUI-Owl Adapter sees the current Android screenshot and proposes one
  `mobile_use` action at a time. The Windows ADB Adapter transports that action
  and captures a fresh post-action frame.

Legacy Workflow, Run, and Approval routes remain backend compatibility
surfaces. They are absent from the primary frontend information architecture,
and neither MobileTask nor Chat silently claims queued legacy Runs.

```text
Browser UI (localhost)
        |
        v
Control-plane HTTP Adapter
        |
        +-- ApplicationRuntime Module
        |      +-- durable ApplicationInstance / cycle / intent / Outcome ledger
        |      +-- content-bound start and command idempotency
        |      +-- one serialized coordinator + revision/dispatch fence
        |      +-- inspect-only unfinished-intent reconciliation / no replay
        |      `-- soul-reply-v1 Profile
        |             +-- dating-copilot owner observation/reserve/dispatch/inspect
        |             +-- loopback local visual-facts Adapter
        |             +-- transient cloud reply Policy
        |             +-- owner-proof Verifier
        |             `-- delayed-outcome reply strategy store
        +-- MobileTaskRuntime Module
        |      +-- durable TaskState / TaskPlan / Subgoal ledger
        |      +-- one coordinator and bounded task queue
        |      +-- sequential local GUI-Owl role-model Adapter
        |      +-- TaskSession-wide Android DeviceExecutionLease
        |      +-- single-image BEFORE / AFTER evidence summaries
        |      +-- zero-image summary-to-summary Verification
        |      +-- conditional exact-frame false-progress guard
        |      +-- bounded three-no-progress Reflection
        |      `-- versioned SkillMemory + local evidence store
        +-- CloudChatConfiguration
        |      +-- redacted settings / connection-test API
        |      +-- Windows-user DPAPI secret protection
        |      `-- hot-swapped provider generation
        +-- ChatCoordinator Module
        |      +-- SQLite chat journal
        |      +-- same-Turn revisioned input inbox
        |      +-- provider / automation revision fences
        |      +-- local text Provider Adapter
        |      +-- cloud dialogue/planner Provider Adapter
        |      `-- AndroidAutomationFactory
        |             +-- local GUI-Owl multimodal Adapter
        |             +-- open-ended sequential screenshot/action loop
        |             +-- cancel / failure / model-terminate end conditions
        |             `-- restricted ADB Adapter
        +-- GameLearner Module
        |      +-- versioned LearningProfile catalog
        |      +-- bounded LearningEpisode + Transition ledger
        |      +-- evidence-gated OutcomeVerifier + RewardSignal
        |      +-- trajectory distillation / versioned PolicyMemory
        |      `-- independent learning.db + local evidence store
        +-- legacy SoulIntegration read Adapter
        |      `-- compatibility workspace / conversation diagnostics only
        +-- target discovery Interfaces
        +-- runtime capability probes
        `-- legacy Workflow / Run / Approval compatibility routes
```

## Deep modules and seams

`ApplicationRuntime` is the generic deep Module for a persistent application
cycle. Its caller Interface is:

```text
ApplicationRuntime(database_path, *, profile, observation_port, policy,
                   execution_owner, verifier, memory_gate=None,
                   memory_scope=None, persistence_projection=None,
                   queue_capacity=32)

start(profile_id, client_request_id, target_id=None, initial_input=None)
    -> ApplicationInstance
command(instance_id, Input|Pause|Resume|Stop, client_request_id)
    -> ApplicationInstance
inspect(instance_id) -> ApplicationInstance
list(limit=100) -> list[ApplicationInstance]
shutdown(timeout=5) -> None
```

The Profile supplies the internal seams, not the caller: `observe(instance)`,
optional `observe_after(instance, intent, receipt)`, `decide(context)`,
`reserve(instance, intent)`, `dispatch(reservation_id, instance, intent)`,
optional `reconcile(instance, runtime_intent)`, `verify(context)`, and an
optional memory gate. Production and test Adapters cross the same seams.

The runtime persists an intent before reserve, then takes one dispatch lock for
the final revision check, owner reserve, owner dispatch and receipt settlement.
`Input`, `Pause`, and `Stop` cannot be acknowledged in the middle of that
physical commit fence. Recovery sends any unfinished intent to `reconcile`
before a new policy cycle; reconciliation may inspect, but never dispatch that
intent again.

The public HTTP Adapter is deliberately smaller:

```text
POST /api/v1/application-instances
GET  /api/v1/application-instances?limit=100
GET  /api/v1/application-instances/{instance_id}
POST /api/v1/application-instances/{instance_id}/commands
```

Create accepts `profile_id`, content-bound `client_request_id`, optional
`target_id`, and optional `initial_input`. A command accepts `command` from
`Input|Pause|Resume|Stop`, a content-bound `client_request_id`, and non-blank
`content` only for `Input`. Both POST routes also require
`X-AI-Game-Client: console-v1`; a missing or invalid header is rejected before
the runtime accepts work. Exact retries return the existing instance; the same
request ID with a different normalized payload conflicts. Responses expose only
lifecycle facts, revision, risk/degradation flags, counts, redacted intent
phases, Outcome classifications and timestamps—never application input bodies,
observations, draft text, owner receipt internals or evidence bodies.

`MobileTaskRuntime` is the default deep Module for generic Android objectives.
Its narrow caller Interface is `start(goal, client_request_id, target_id?,
skill_id?)`, `send(task_id, content, client_request_id)`,
`stop(task_id, client_request_id)`, `inspect(task_id)`, `list(limit)`, and the
process-lifecycle hook `shutdown(timeout)`. The Module owns the state machine,
durable idempotency, task admission, orderly quiescence, restart recovery,
model-role sequencing, physical-intent journal, verification, Reflection, and
optional SkillMemory promotion. That Depth keeps high Locality: the HTTP Adapter
and browser translate owner intent but do not reproduce planner, verifier,
queue, scope, shutdown, or recovery decisions. When the active runtime cannot be
composed, a read-only `MobileTaskArchive` seam serves list/inspect from the same
database without accepting writes.

The main internal seams are a role-model Interface, a `TaskSession` driver
Interface, a `SkillScopeResolver`, the SQLite store, and the evidence store. The
production role-model Adapter calls the same loopback GUI-Owl-compatible
endpoint sequentially as Planner, Executor, BEFORE summarizer, AFTER summarizer,
zero-image Verifier, and Reflection; it does not create resident role agents and
does not use the cloud chat provider. The production Android driver
Adapter binds a requested ready Android Target by deriving an executor for that
Target's discovered serial; when omitted, it falls back to the configured
default ADB serial. MobileTask can be composed without that fallback, so an API
request that omits a Target may be accepted and later fail asynchronously with
`executor_not_configured`; the primary UI requires a ready selected Target.
Discovery is read-only and exposes emulator/USB/wireless
connection type plus screen-capture, touch-input, and ASCII-text capabilities.
The TaskSession does not silently switch targets after binding. It acquires a
process-local `DeviceExecutionLease` before opening the TaskSession and holds it
for that entire session. A normal claimed run keeps one session across planning,
actions, verification, and Reflection; orderly shutdown may close it at a safe
nonterminal `queued` checkpoint, and recovery opens and leases a new session.

One daemon coordinator serializes all MobileTask model and device work. The
production queue admits at most 32 outstanding tasks, and a claimed task stays
with the coordinator through final runaway guards of 2,048 ActionAttempts and 64
Reflections.
Each task follows one sequential control loop:

```text
current goal + ordered inputs + optional latest SkillMemory
→ Planner creates a revisioned TaskPlan and ordered Subgoals
→ fresh BEFORE observation
→ Executor proposes act, finish, or terminate
→ persist an open ActionAttempt and full physical intent
→ atomically fence stop and input_revision immediately before dispatch
→ dispatch at most one physical action
→ allow an accepted physical action one second to settle
→ fresh AFTER observation
→ summarize visible facts from the single BEFORE image
→ summarize visible facts/obstruction from the single AFTER image
→ Verifier sees both text summaries + exact-frame flag, and no image
→ atomically advance TaskState, redecide, reflect, or terminate
```

Transport acceptance is not verification. A Subgoal advances only on a current-
revision `satisfied` Verification based on fresh evidence, and the Task can
become `completed` only after every Subgoal is satisfied. Three consecutive
no-progress results invoke Reflection before another proposal. Reflection may
change strategy, replace the plan with a new revision, or fail the task; once
the configured 64-Reflection runaway guard is exhausted, the task fails closed.
A physical or verifier uncertainty terminates the Task as `uncertain`.

Verification is deliberately a three-call Adapter sequence. The BEFORE and
AFTER summarizers each receive exactly their own single image; the final
Verifier receives no image and compares only their bounded textual facts, the
AFTER `goal_obstructed` signal, and the local exact-frame result. No model request
contains both frames. The Adapter also compares image dimensions and PNG bytes.
When frames are exact matches and the Verifier did **not** establish
satisfaction, a claimed `progress=true` is deterministically reduced to
`progress=false`. Exact identity is not itself failure evidence: if the text
summaries already prove a static final state and the Verifier returns
`satisfied=true / progress=true`, that result remains valid. Visible AFTER
obstruction still prevents satisfaction. A byte change is not sufficient proof
of progress; the Verifier still needs visible evidence. The one-second physical
settle is only a refresh allowance, not application-idle or success proof.

`send` and `stop` share the final physical-dispatch seam. If either wins before
dispatch, the persisted intent is finalized `not_sent`; if dispatch already
won, the atomic device action cannot be recalled. A newer input or stop that
arrives while AFTER verification is in flight preserves the attempt's real
transport and evidence facts but prevents that stale result from advancing a
Subgoal or promoting SkillMemory. MobileTask input lifecycle is only
`accepted`/`applied`; it is intentionally different from Chat Message
`queued`/`applied`/`rejected`.

`ChatCoordinator` is the dialogue/execution Module. Its public Interface creates
and lists Sessions, accepts idempotent user input, returns a transcript snapshot,
and cancels a Turn. An idle Session creates one Turn and worker. Input accepted
while that Turn is `accepted`, `queued`, `thinking`, `planning`, or `executing`
joins the same Turn, advances `input_revision`, and wakes no second worker;
`stopping` rejects new input. The Module hides provider routing, background
scheduling, SQLite transactions, reply/execution status separation,
cancellation, and device automation. HTTP routes do not call a model or ADB
directly.

Before each provider decision, the worker atomically moves queued user Messages
to `applied` and captures their Turn revision. Persisting a provider reply uses
that revision as a compare-and-set fence. If newer input wins the race, the old
reply is not persisted and the same worker takes a new snapshot and replans.

`OpenAIChatProvider` hides the text-provider dialect. Session mode is immutable,
so text entered in a local-only session cannot later be sent to the cloud by a
mode toggle.

`CloudChatConfiguration` is the single runtime owner for the cloud endpoint,
model, credential and provider generation. The browser settings page can read a
redacted view, save an OpenAI-compatible route, run an explicit connection test,
or clear the route. API keys are protected with Windows DPAPI for the current
Windows user before the protected blob is persisted; there is no plaintext
fallback, and neither the API nor browser receives the saved value back.
Optimistic revisions reject concurrent stale writes. A successful save or clear
replaces the provider used by subsequently created Turns without restarting the
console; an already-running Turn, including later input appended to it, retains
the generation it began with.

`config/cloud-runtime.env` and the process environment are startup bootstrap
inputs only when no console-managed record exists. The file remains non-secret,
and an optional bootstrap key may come only from the process environment. Once
the operator saves or explicitly clears the settings-page configuration, that
persisted revision wins on later starts.

`RepositoryAndroidAutomationFactory` binds a persisted Android target to the
currently configured dynamic ADB serial. A stale target is rejected instead of
being redirected to a different emulator endpoint.

`GuiOwlAndroidAutomation` owns the production chat physical loop. It has no
fixed action-count ceiling, but each iteration remains sequential:

```text
fresh screenshot
→ local GUI-Owl receives screenshot + goal + text-only action history
→ strict official mobile_use envelope parser
→ cancellation and bound-target check
→ one restricted ADB action, or redirect legacy interact back to replanning
→ fresh post-action screenshot
→ repeat until user cancellation, device/model failure, or model terminate
```

The loop emits persistence-safe timeline events. They contain no screenshot
bytes, model response bodies, typed text, credentials, or API keys. An ADB
transport acknowledgement is recorded separately from the post-action
observation and is never treated as application-level completion.

The MobileTask HTTP projection is intentionally smaller than its local journal.
ActionAttempts omit physical intent arguments, coordinates/typed text,
BEFORE/AFTER observations and evidence IDs, transport receipt IDs/details, and
the internal state-advance arbitration value. Events expose only sequence, type,
and time. The owner-supplied goal and input content, plan/subgoal descriptions,
strategy and Reflection text, verifier evidence text, and sanitized error/detail
remain inspectable. Follow-up input `client_request_id` values are public with
those inputs; create and stop request IDs remain internal. There is no HTTP route
for downloading MobileTask evidence.
The next Executor decision also receives no raw recent arguments: it sees at
most eight redacted action fingerprints with transport/result context. Tap and
long-press retain only a coarse 4x4 screen region, swipe only direction, typed
text becomes `text(redacted)`, and an allowed keyevent retains only its key
identity. This supports repeat avoidance without leaking exact coordinates or
typed action text through the attempt history.

During Android execution, later applied Messages become ordered updates to the
current goal. Each GUI proposal is checked against the instruction revision
after model output and again immediately before physical dispatch; `terminate`
is checked before it can end the run. A stale proposal or termination is
discarded and the same worker replans from a fresh observation. Input arriving
after the final pre-dispatch check is visible at the next decision boundary but
cannot recall an atomic input already handed to ADB.

`GameLearner` is a separate deep Module for bounded, low-frequency learning. Its
complete caller Interface is lifecycle (`start`, `shutdown`), profile discovery
(`list_profiles`), and job control (`learn`, `list_jobs`, `inspect`, `stop`). It
hides one finite `LearningEpisode`, the append-only `Transition` ledger, local
evidence storage, outcome verification, reward derivation, trajectory
distillation, and versioned PolicyMemory promotion. HTTP routes never call the
verifier, artifact store, or distiller directly.

The first implementation does not update model weights and does not persist a
candidate/validation phase. When one episode has confirmed success and at least
one positive physical Transition, deterministic trajectory distillation inserts
an immutable PolicyMemory revision and promotes it in the same transaction;
otherwise policy remains unchanged. `distilling`, `validating`, `candidate`, and
`rejected` are reserved for a future separately inspectable trainer/LoRA flow.
`TrainingJob` and a LoRA model Adapter are future, separately governed modules,
and their weight candidates must never activate merely because they were
created. `stzb-tutorial-v1` is positively scoped to a fixed, authorized tutorial
and read-only menu catalog, not general competence in 率土之滨.

Production composition injects `StzbAndroidEnvironmentFactory` behind the
GameLearner environment seam when the configured ADB executor and loopback local
GUI model are available. Profile discovery remains a description of scope, not
a readiness probe: if those dependencies are absent, episode opening fails
closed instead of falling back to the production chat loop or arbitrary ADB.

The composition root creates one non-blocking `DeviceExecutionLease`, keyed by
canonical ADB serial, and injects that same instance into MobileTask, ordinary
Android Chat automation, and the LearningEpisode Adapter. MobileTask holds it
for the entire TaskSession, including planning, observation, execution,
verification, and Reflection; Chat and Learning hold it for their own physical
session boundaries. A held target fails with `target_busy` before a competing
path calls its device/model loop, so these three paths cannot interleave on one
target inside this Python process. This lease is process-local only: it does not
coordinate another console process, direct ADB, external device tools, or the
separately owned dating-copilot controller.

Soul is the production `ApplicationRuntime` Profile `soul-reply-v1`. AI-GAME
owns the long-lived instance, observation/policy/verification loop, local visual
interpretation, transient cloud draft, revision fence and delayed reply-strategy
learning. `F:\dating-copilot` remains the sole device and physical-ledger owner;
it owns stable transcript/revision capture, exact conversation identity,
pre-click validation, one physical send, echo proof and uncertain-send
reconciliation. Neither process opens the other's database or controls the
other's process lifecycle. Explicit Application lifecycle commands may change
only the scheduler desired state exposed by the owner HTTP contract.

The Soul gateway keeps dependency probe and runtime factory calls outside its
state lock so those transports cannot hold the shutdown fence. A dedicated
initialization lock makes construction single-flight. Closed state is checked
after the probe and again before publication; if shutdown wins the race, the
late candidate is bounded-shutdown and never becomes the active runtime.

`SoulOwnerClient` uses this loopback-only owner v1 Interface:

```text
GET  /api/application-owner/v1/capabilities
POST /api/application-owner/v1/soul/observations
POST /api/application-owner/v1/soul/intents
POST /api/application-owner/v1/soul/intents/{owner_ref}/dispatch
GET  /api/application-owner/v1/soul/intents/{owner_ref}
GET  /api/application-owner/v1/soul/application-intents/{application_intent_id}
GET  /api/application-owner/v1/soul/scheduler
PUT  /api/application-owner/v1/soul/scheduler
```

Scheduler GET returns exactly `contract_version`, `controller_ref`,
`desired_state`, `effective_state`, `reply_owner`, and `scheduler_mode`. PUT
contains exactly contract v1, desired state `running|paused|stopped`, and fixed
controller ref `ai-game-soul-reply-v1`. This is a managed matcher seam, not
process control or a general dating-copilot command proxy.

Observation request body is `{"contract_version":"v1"}`. A due-inbound
response binds one bounded PNG, stable transcript, opaque `scope_ref`,
`conversation_revision`, `conversation_ref`, `pending_generation_ref`, and
`transcript_revision`; an idle response is explicitly
`scope=no_due_pending_inbound`. AI-GAME gives the PNG only to the loopback local
vision Adapter, removes it before policy/persistence handoff, and sends the
cloud provider only transcript text, structured visual facts, strategy and the
latest owner instruction.

There is no observation DELETE/abandon operation. A new observe may atomically
replace only an unreserved process-local scope; the old scope then becomes
`observation_scope_stale`. Reservation-in-flight, reserved, or active pipelines
are not replaced. `foreground_action_owned` becomes a retryable Application
wait rather than permission to dispatch an old draft.

Reserve body contains `contract_version`, deterministic
`application_intent_id`, `scope_ref`, and exactly one `draft.text`. Dispatch
repeats the same draft plus `scope_ref` and
`preflight.conversation_revision`, allowing dating-copilot to verify immutable
content and the same observation revision after either process restarts. The
owner's `confirmed` result is re-inspected and verified. An
`uncertain_needs_reconciliation`, proof mismatch, transport interruption or
open intent goes through inspect-only reconciliation; it is never re-dispatched.
The bounded owner status vocabulary is `reserved`, `reserve_replayed`,
`reserve_rejected`, `confirmed`, `active_dispatch`,
`uncertain_needs_reconciliation`, `terminal_no_replay`,
`stale_preflight`, `preclick_rejected`, `legacy_scheduler_active`,
`soul_execution_runtime_unavailable`, and
`fresh_owner_observation_unavailable`. Only the explicit confirmed path is
delivery success. `legacy_scheduler_active` is a refusal to mix the old
unmanaged worker/mode with the managed owner Interface, never a fallback
instruction.

`active_dispatch` is a nonterminal owner operation: reconciliation waits and
repeats GET/inspect only, never reserve, dispatch, or draft generation. If the
original dispatch receipt is `active_dispatch`, the first after-observation GET
is defer-only even if its current status already changed; the second and later
GETs provide authoritative reconciliation. A direct `legacy_scheduler_active`
or `soul_execution_runtime_unavailable` response is definite-not-sent but still
settles through an owner GET returning `terminal_no_replay`; the resulting
nonterminal `confirmed_failure` permits a fresh observation/re-plan without
replaying the old intent or draft.

The scheduler and reply loop have independent responsibilities. The managed
dating-copilot scheduler uses match opportunities throughout the day, opens a
successful match immediately, performs bounded unread discovery, and reaches
Planet only after trustworthy same-day zero quota. It does not draft or send
ordinary replies; `soul-reply-v1` owns that work and delayed learning.

`soul-scheduler-lifecycle.db` is a content-free singleton control receipt. The
complete Soul archive is aggregated across instances: any nonterminal
`queued`/`running`/`waiting` demand, including a `stopping` instance whose
pre-Stop target was running, selects running; otherwise any paused or
pre-Stop-paused demand selects paused. With no nonterminal instance, the newest
explicit lifecycle evidence applies, but Stop selects stopped only when its
instance is durably `stopped`. Failed/completed instances preserve their latest
explicit target and cannot make an older stopped target authoritative again. A
monotonic generation and aggregate hash prevent stale lifecycle replay from
rolling the shared decision back.

One interruptible reconciler serializes lifecycle commands with scheduler
control, performs GET before a necessary idempotent PUT, and rechecks the
generation before writing. A lost response is inspected on the next pass rather
than compensated. Owner outage backs off; a dating-copilot reverse restart is
repaired from durable desired state without a new Application instance. Matcher
reconciliation remains available when reply cloud or local vision dependencies
are offline. Cold paused recovery allows desired paused/effective stopped and
does not briefly start matching. Gateway shutdown closes local activation, the
monitor, and any loaded core runtime. The reconciler rechecks closed state after
owner GET and before PUT, so shutdown never sends stopped or another late
scheduler write.
If reply dependencies are unavailable during cold recovery, the reconciler may
delegate an idle Stop compare-and-set to the dependency-free Application core.
It never clears a worker token or unfinished physical intent; those instances
remain stopping and keep their pre-Stop target.

The Soul persistence projection stores commitments and status facts rather than
message bodies or draft text. `soul-reply-learning.db` separately binds trial,
conversation/pending/transcript revisions, draft hash, selected strategy,
prompt/persona/provider/model versions, owner ref and send proof. Confirmed send
is delivery proof, not effectiveness. `SoulReplyMemoryGate` rejects immediate
promotion; only a later new inbound or an explicitly evidenced delayed
positive/negative/no-response result updates reply strategy. Learning-trial
storage must succeed before owner reserve. Once reserve or delivery is durable
at the owner, local owner-binding and send-proof writes are best-effort; their
failure leaves repairable lineage and cannot downgrade delivery, suppress the
one core-fenced dispatch, or authorize a second reserve/send.

The browser manages Soul through `/api/v1/application-instances` and reads the
separate safe scheduler projection at
`GET /api/v1/application-profiles/soul-reply-v1/scheduler`. That projection
contains only profile, `state`, desired/effective states, controller match,
stable code, and observation time; it has no scheduler write route, controller
ref, identity, or message body. The legacy
`GET /api/v1/integrations/soul` and conversation-detail GET remain read-only
diagnostics. Both old POST command routes—`/api/v1/integrations/soul/commands`
and `/api/v1/soul/commands`—return `410 legacy_soul_write_disabled`. The old
`SoulIntegration` receipt ledger is therefore compatibility history, not the
main runtime path.

## Persistence and recovery

SQLite schema version 2 added independent chat Sessions, Turns, Messages and
timeline steps while retaining version 1 run data. Version 3 added the
console-managed cloud configuration revision and DPAPI-protected credential
blob. Version 4 adds `chat_turns.input_revision` and the
`chat_messages.client_request_id`, `content_sha256`, `input_revision`,
`delivery_status`, and `applied_at` columns plus their request/revision indexes.
The conditional in-place v3 migration preserves each earlier user Message as
revision 1 with `delivery_status=applied` and `applied_at=created_at`. Model calls
and device actions never hold a database write transaction.

Only one non-terminal Turn and worker may run per Session. Each accepted input
has a content-bound `client_request_id`, so retries are idempotent even when the
Message joined an existing Turn. `queued` means durable but not yet read;
`applied` means included in one model-decision snapshot, not replied to, acted
on, or completed; `rejected` means cancellation, stop, terminal failure,
shutdown, or restart finalized the Turn before the worker read it. On restart,
interrupted Turns are finalized rather than replayed, and physical ADB actions
are never automatically resent after uncertain process termination.

MobileTask persistence is independent schema-v2 SQLite state in
`runtime/console/mobile-tasks.db`. It stores Tasks, content-bound request IDs,
historical Plan revisions, inputs, full intent-before-dispatch ActionAttempts,
Reflections, Events, internal `skill_scope_id`, and immutable SkillMemory
versions. Request IDs are global to this store and bound to operation plus
payload: an exact retry returns the owning Task, while reuse for another
operation, task, or body conflicts. The in-place v1-to-v2 migration moves prior
explicit task/memory keys into the `legacy:` namespace and adds the internal
scope without calling a model or device.

`MobileTaskRuntime` performs recovery and starts its single daemon coordinator
during construction. Recovery applies fixed precedence: a persisted but
unfinalized physical `act` intent first makes the Task terminal `uncertain` with
`restart_open_intent` and is never replayed; otherwise a stopping or
cancel-requested Task becomes `stopped`; otherwise a safe active checkpoint
returns to `queued` and is automatically resumed. Recovery beyond the bounded
queue fails explicitly instead of creating unbounded work.

Orderly `shutdown(timeout)` closes the mutating task Interface and shares the
same dispatch fence. A persisted but undispatched intent is settled `not_sent`;
an action already dispatched is allowed to finish its one-second settle, fresh
AFTER capture, Verification, and durable settlement, after which no new action
starts. A safe unfinished Task is left `queued` for constructor recovery. A
timeout is reported rather than pretending shutdown completed; archive reads
remain available.

MobileTask screenshot evidence is stored locally as opaque PNG plus dimension
metadata under `runtime/sessions/mobile-tasks/evidence/`; SQLite holds the
references. On every new record, the evidence-store Implementation best-effort
removes incomplete and age-expired pairs and trims the oldest pairs to default
global bounds of 256 frames, 1 GiB, and 7 days while always retaining the new
pair. Those bounds are therefore best-effort rather than absolute for the newest
pair. There is no background cleanup timer, and durable Task history may outlive
raw frames.

Completing the final Subgoal may atomically add `max(version)+1` SkillMemory.
An explicit public `skill_id` maps to an internal `legacy:` scope. If it is
omitted, the production `SkillScopeResolver` selects an `auto:` namespace from
the normalized goal: scoped 率土之滨 launch/tutorial/daily/general families or a
generic exact-goal hash; it returns no MobileTask scope for Soul because the
`soul-reply-v1` Profile owns separate delayed-outcome reply learning. Automatic
scope ignores `target_id` by
policy, permitting procedure reuse across targets without claiming those
targets were tested. Procedure, strategy, and evidence come only from
satisfied, state-advancing attempts. This is versioned procedure reuse,
separate from GameLearning PolicyMemory and from model-weight training.

Large screenshots remain ephemeral in the production chat loop. If durable chat
visual evidence is added later, bytes belong under `runtime/sessions`, with only
hashes and relative references in SQLite.

Game learning already follows that storage split. Its bounded metadata,
Transition ledger, outcomes, rewards and PolicyMemory versions live in the
independent `runtime/console/learning.db`; evidence bytes and derived artifacts
live under `runtime/sessions/game-learning/`. The existing `console.db` schema remains a
separate control-plane/chat contract.

Learning recovery never replays a Transition or physical action. A non-terminal
job found after shutdown or restart is finalized through explicit recovery or
stop semantics. V1 has no persisted PolicyCandidate; a future candidate must
never be promoted merely because the process started again. Transport
acceptance remains separate from the OutcomeVerifier's evidence-backed
classification.

## Test-mode continuation and termination

The production chat device loop currently runs in a deliberately open test
mode. Clicks, long presses, swipes, restricted text input, Back/Home/Menu and
waits run without per-step approval. Credentials/passwords, OTP/biometrics,
identity verification, payment, CAPTCHA, system permission, legal consent and
ambiguous pages are not content-classified into automatic pauses.

There is no fixed step count. Once device execution starts, only explicit user
cancellation, a device/ADB or model failure, or the local GUI model's
`terminate` action ends the loop. Malformed or unsupported model output is a
model-side failure, not a request for manual page handling. Cancellation is
checked before later physical inputs, but it cannot undo an atomic action that
ADB already accepted.

Legacy GUI-Owl variants may still emit `interact`. In test mode this is recorded
as a `redirected` timeline event and the next model call is explicitly required
to choose a physical action from a fresh observation; it does not create an
`awaiting_user` handoff. Historical `paused` results remain readable for schema
compatibility only.

This continuation policy is a test-mode operating choice, not a safety claim.
Operators must scope runs to targets and flows they explicitly authorize and
use the Stop control when needed. The runtime does not promise to recognize or
protect real credentials, money movement, account changes, permission grants,
legal consent, CAPTCHA, or other consequential screens.

## Runtime layout

```text
apps/console/backend/         Python API and control-plane implementation
apps/console/frontend/        React browser UI
apps/console/tests/           backend and frontend tests
contracts/                    stable cross-module contracts
config/model-runtime.env      pinned local GUI model route
config/executor-runtime.env   ADB executable, enablement, optional default serial
config/cloud-runtime.env      optional non-secret startup bootstrap hints
runtime/console/console.db    generated SQLite state
runtime/console/learning.db   independent game-learning state
runtime/console/mobile-tasks.db independent MobileTask state and SkillMemory
runtime/console/application-runtime.db generic ApplicationInstance cycle ledger
runtime/console/soul-scheduler-lifecycle.db content-free matcher target receipt
runtime/console/soul-reply-learning.db Soul trial lineage and delayed outcomes
runtime/console/soul-integration.db legacy SoulIntegration compatibility data
runtime/sessions/game-learning/ local LearningEpisode evidence and artifacts
runtime/sessions/mobile-tasks/evidence/ local MobileTask PNG/size evidence
runtime/logs/                 owned process logs
runtime/run/                  owned PID files
scripts/console.ps1           setup/build/start/stop/status/test entrypoint
```

The HTTP server binds to `127.0.0.1` by default and serves the built frontend
from the same origin. Raw Android screenshots are only accepted by a GUI-Owl
client whose endpoint resolves syntactically to loopback.

## Current exclusions

- no Windows mouse/keyboard automation Adapter yet;
- no emulator create/delete lifecycle control;
- no semantic sensitive-screen hard-stop or safety guarantee for credentials,
  OTPs, biometrics, CAPTCHA, payment, permission or legal-consent flows;
- no cloud screenshot upload;
- no LAN exposure or multi-user authentication;
- no claim that model termination proves an application-internal business
  outcome;
- no automatic execution of legacy saved runs;
- no claim that Mobile-Agent is installed or acts as a parent runtime;
- no model-weight training, LoRA creation/activation, or claim that a completed
  LearningEpisode means a game objective or general game skill was learned;
- no continuous visual state estimator, high-frequency local game controller,
  multi-pointer transport, or evidence of 3–5 minute gameplay endurance; the
  future training/custom/sandbox acceptance line is documented in
  `docs/gameplay-readiness.md`.

The primary browser information architecture has exactly four entries:
one-sentence start, Soul, Device, and Settings. Chat and
GameLearning APIs/history, and legacy Workflow, Run, and Approval routes and
records, remain backend compatibility Interfaces rather than first-level
workspaces and are not executed by MobileTask. There is no implemented
universal Run or router that converts an arbitrary MobileTask sentence into
Chat, LearningJob, or Soul operations. Source code, automated tests, built assets,
code loaded by a running process, real-device verification,
SkillMemory/PolicyMemory promotion, and gameplay-readiness evidence remain
separate facts. This
architecture document is not evidence that Mobile-Agent is installed or that a
particular local model/device is currently ready. One supplied real-task record
does establish only this narrow fact: Task
`daac81a7-1af9-47e3-9566-66e73509a0fd` completed after 23 ActionAttempts under
`auto:stzb/tutorial/v1`. It does not establish general 率土之滨 competence. The
new `soul-reply-v1` reply chain still requires this round's live acceptance.
