# Decisions and open questions

## 1. Confirmed decisions

### D1 — Universal product boundary

AI-GAME is a universal local mobile AI operator. Games, Soul, settings, and future applications are environments/capabilities, not separate products.

### D2 — One primary goal entry

The ordinary user states an outcome once. Internal runtime, profile, skill, model, device, and route selection are platform responsibilities.

### D3 — Full operational delegation

AI-GAME should inspect, configure, plan, operate, verify, recover, learn, and continue automatically. It asks only for an unavoidable external gate or a material product choice.

### D4 — Local AI direction

The local Qwen3.8 27B deployment is intended to receive the user command and arrange the work. Model binding details remain configurable.

### D5 — Repeat-run learning is vNext core

Success, verified failure, wrong-page transition, recovery, and delayed outcome must contribute to scoped reusable experience. Improvement must be measured, not inferred from a memory row.

### D6 — Development-first autonomy

The first platform loop does not inherit broad keyword restrictions from the old STZB tutorial profile. Business safety policy is layered later.

### D7 — Runtime correctness remains mandatory

Device ownership, user stop, revision/observation fences, action validation, fresh verification, no uncertain replay, bounded recovery, and evidence are correctness, not optional content restrictions.

### D8 — Old product baseline is replaceable

Older definitions, boundaries, frozen design documents, and roadmap rules may be deleted or superseded. This package is authoritative.

### D9 — Current modules migrate incrementally

MobileTask, RuntimeKernel, ApplicationRuntime, GameLearning, Soul, and Chat are not merged in one rewrite. The current real MobileTask loop remains a compatibility engine until Kernel proves an equivalent full goal and rollback.

### D10 — External outcomes are represented honestly

The platform autonomously pursues controllable steps but cannot guarantee another person's decision or an unavailable external event. Such goals use waiting, partial, candidate notification, optional user takeover, and user-confirmed result semantics; a notification does not terminate a continuous goal.

### D11 — Model roles use a measured default

Confirmed by the user on 2026-08-20:

- Qwen3.8 27B is the default commander for goal understanding, success criteria, planning, routing, reflection, memory reasoning, and language;
- GUI-Owl is the initial visual operator for current-screen grounding and one atomic phone action;
- Runtime/hybrid verifiers own immediate and final truth;
- if the actual Qwen binding supports vision, U3 evaluates it against GUI-Owl on the same controlled and real-device scenarios and selects the better proven visual binding rather than preserving a weaker model split by doctrine.

Resolved by U3 on 2026-08-21: Qwen is the current visual default. On five
identical persisted real-device frames it produced 5/5 successful-path decisions;
GUI-Owl produced 3/5 and failed the finish/Home cases. Qwen was slower, so latency
remains an optimization target, not a reason to select the less correct binding.

The comparison records grounding/action validity, wrong/no-effect actions, completion, latency, recovery, and resource use. Generic runtime contracts remain capability-based, so an evidence-backed binding change does not rewrite GoalRun or Kernel.

### D12 — One approval for a missing-install plan

Confirmed by the user on 2026-08-20:

- AI-GAME may inspect, start, reconnect, repair, and reversibly configure already installed components it owns without asking;
- when a required model, emulator, or software component is missing, or an operating-system security setting must change, AI-GAME first presents one concrete installation/change plan and asks once;
- after approval, it downloads, installs, configures, starts, verifies, and resumes the same GoalRun without step-by-step questions;
- materially expanding that approved plan, accepting new third-party terms, or changing a different security boundary requires a new one-time approval.

### D13 — Official acceptance order

Confirmed by the user on 2026-08-20:

1. settings/battery through the universal entry, proving general phone operation and configuration;
2. STZB daily repeat-run learning, proving recovery and measurable improvement;
3. a long-lived application/social goal, proving event waits, continuity, delayed outcomes, notification, and control.

### D14 — Long-lived goals continue until explicit stop

Confirmed by the user on 2026-08-20:

- a long-lived matching/conversation goal does not terminate or automatically pause merely because a promising candidate is found;
- the platform records and notifies candidate milestones while continuing under the active goal;
- it stops normal operation only when the user explicitly stops, takes over, or revises the goal, or waits when a real external/configuration gate prevents progress;
- each individual interaction cycle still has action, time, retry, and recovery bounds, with event-driven waits and backoff between cycles; “continue until stopped” never means an unbounded click loop;
- only the user can confirm a real relationship or comparable external-world outcome.

### D15 — Qwen multimodal single-resident tuning baseline

Confirmed by the user on 2026-08-21 and bounded by U3 acceptance:

- use the installed Qwen3.8 27B multimodal binding as the current tuning baseline
  for commander and visual roles;
- keep planner, one-action operator, verifier, and reflector as separate forced-tool
  contracts even when one model process serves all roles;
- on the inspected RTX 3090 24 GiB machine, tune one resident Qwen process first;
  Qwen Q5 and the repository GUI-Owl service do not fit concurrently, so any
  comparison is serial rather than dual-resident;
- U3 comparison has now promoted this baseline to the current accepted visual
  default; changing it again requires new comparable evidence;
- configuration remains machine-local and secret-free in tracked files.

U4 tuning note: forced-tool GoalSpecification now uses the same non-thinking,
zero-reasoning-budget request shape as Goal Completion. This is a reliability
tuning inside the accepted Qwen binding, not a model-role change. A failed warm
run caused by a later model-service outage remains failed and unpromoted.

### D16 — Owner-authorized U5 closeout and U6 advancement

Confirmed by the user on 2026-08-23:

- close U5 quickly without pretending its missing real complete-manifest,
  whole-set reread, and cold/warm learning evidence exists;
- reduce documentation rules that made every remaining U5 stabilization item a
  hard blocker for the total roadmap;
- move those missing U5 facts to a named non-blocking stabilization backlog;
- activate U6 and execute one bounded, reversible Kernel settings/battery
  canary;
- retain every non-negotiable correctness gate, especially one physical owner,
  user control, revision/current-observation fences, fresh after evidence, no
  uncertain replay, independent final completion, and rollback to
  `mobile_task_compat`.

This is a scheduling and advancement decision, not a retroactive lowering of
U5 acceptance or permission to claim U5 `DONE`.

### D17 — U7 local observation acceptance

Confirmed through the user's U7 observation execution instruction on
2026-08-23:

- define and execute the remaining U7 observation, close U7 only after it
  passes, then activate `U8_LONG_LIVED_ROUTING` under the ordinary rule;
- use a 30-minute local wall-clock observation with a health/ownership sample
  every 30 seconds and one controlled normal Kernel restart after 15 minutes;
- every sample must keep the managed console and database healthy, runtime
  capabilities ready, `kernel_active=true`, `legacy_writable=false`, zero
  active Legacy work, and—because this is an isolated idle observation—zero
  active Kernel tasks and active Leases;
- the controlled restart must restore the owned current-source Kernel process
  within 30 seconds with a new listener identity, append the full mode-log
  stop/composition/start sequence, retain `runtime_kernel`, and keep Legacy
  workers stopped;
- before and after restart, a fenced Legacy write must return the explicit 403
  contract while the Legacy compatibility archive, Kernel tasks, and GoalRuns
  remain readable;
- append-only cutover/mode logs must not shrink, and the observation must not
  record a fatal/unhandled console error or a new physical action.

[constraint-source: USER_DECISION; ref: U7 observation execution instruction 2026-08-23]

This is a bounded local U7 cutover acceptance definition. It does not claim
packaged deployment, long-run production reliability, U5 STZB acceptance, or
U8 long-lived capability acceptance; those remain separate milestones.

Executed result: the observation passed on 2026-08-23 with 61/61 successful
samples over 1,801.493 seconds, a 3.675-second controlled restart, the required
mode-log sequence, Legacy 403 fences before and after restart, stable read
surfaces, zero active tasks/Leases, and zero fatal errors. U7 closed and U8 was
activated under the ordinary advancement rule.

### D18 — U8 Soul test-account full-autonomy authorization

Confirmed by the user on 2026-08-23 for the currently configured Soul test
account and the active U8 acceptance run:

- AI-GAME may read, analyze, retain the already-defined bounded evidence, and
  display conversation material from this test account when it is relevant to
  U8 verification;
- the long-lived application may autonomously discover candidates, classify
  compatibility, generate replies, and send through the controlled capability
  selected by the frozen GoalRun binding plan, without per-message user
  approval or a manual-chat prerequisite; an already logged-in Android
  application does not thereby require a specialized external owner;
- candidate notification remains informational and must not pause the GoalRun;
- this content and account-operation authorization does not remove runtime
  correctness invariants: one physical execution owner, explicit stop/takeover fences,
  current conversation/revision validation, at-most-once send, no uncertain
  replay, and durable evidence remain mandatory;
- scope ends when U8 acceptance ends, the configured account changes, or the
  user revokes the authorization. Extending the same policy to a production or
  personal account requires a new explicit decision.

[constraint-source: USER_DECISION; ref: U8 Soul test-account full-autonomy instruction 2026-08-23]

This decision removes user-approval and privacy-display gates for the named
test scope. It does not lower U8 evidence thresholds or authorize bypassing a
frozen/uncertain physical-action ledger merely to obtain a passing result.

D22 corrects only the implementation assumption formerly embedded in this
decision: the authorization is attached to the account and GoalRun scope, not
to `soul-reply-v1` or any external-owner architecture. A specialized adapter
adds its own authorization/readiness gate only when the frozen plan selects it.

The rejected `local-managed-v1` checkpoint did not itself end this
authorization. Current applicability must be determined from D18's original
account, U8-scope, change, and revocation conditions. D20 prevents transferring
it to another owner endpoint or account by implication.

### D19 — Project-wide autonomous operation policy

Confirmed by the user on 2026-08-23 for AI-GAME as a whole:

- supported goals run without step-by-step permission prompts, per-message
  approval, manual-chat prerequisites, or content-display restrictions;
- the system may observe, reason, select capabilities, communicate, and
  continue work autonomously within the account, device, and external services
  placed in the active goal's scope;
- user interaction is reserved for missing external authority or a genuinely
  material product choice that cannot be derived from the goal and evidence;
- runtime correctness remains part of the requested product outcome rather
  than an optional permission boundary: ownership, stop/takeover fences,
  current-target validation, at-most-once external effects, uncertain-result
  reconciliation, and truthful evidence cannot be disabled by an execution
  policy.

[constraint-source: USER_DECISION; ref: project-wide no-human-boundary instruction 2026-08-23]

This decision applies to product interaction and autonomous execution. It does
not redefine a duplicate, cross-target, post-stop, or unverified side effect as
acceptable behavior, because doing so would make goal completion and U8/U9
acceptance unprovable.

### D20 — Retired historical Soul-page owner is excluded from U8

Confirmed by the user on 2026-08-23:

- the historical `F:\dating-copilot` Soul-page project is retired and must not
  be started, probed, or treated as the external owner for U8 acceptance;
- D18 does not transfer its historical test-account authorization to a new
  owner, endpoint, or account by implication;
- `soul-reply-v1` may remain an internal optional profile. If a future frozen
  plan actually selects that specialized adapter, the adapter requires its own
  current configuration and authorization; generic RuntimeKernel operation of
  an already logged-in application does not require a replacement owner service;
- D20 does not nominate a replacement owner/account and does not make any
  external adapter a universal prerequisite for the core U8 route.

[constraint-source: USER_DECISION; ref: retired Soul-page project correction 2026-08-23]

User outcome requested: continue U8 according to AI-GAME's universal-goal
vision, rather than reviving an older page project. Technical consequence: the
normal launcher has no implicit legacy-owner endpoint; an absent current owner
is represented as `owner_not_configured` without an owner call. Affected
documents: the U8 work order, current-state/roadmap/index, and ownership
architecture. Effective milestone: U8. Evidence: the user explicitly marked
the historical Soul-page project obsolete before further U8 action.

### D21 — WITHDRAWN: device-free local checkpoint is not U8 acceptance

Status: `WITHDRAWN` by the user's current correction on 2026-08-23. This entry
is retained only as audit history and has no normative effect.

Historical error: the prior D21 treated one `local-managed-v1` checkpoint run
as the U8 long-lived application/social acceptance and used it to mark U8
`DONE` and activate U9. That run is valid lifecycle infrastructure evidence,
but it used no device, network, target application state, real application
event, or delayed reply/no-response outcome. The user rejected that
interpretation as product-route drift.

### D22 — U8 requires a real long-lived mobile/application vertical

Confirmed by the user's route correction on 2026-08-23:

- `local-managed-v1` continuity evidence remains valid supporting evidence; it
  does not establish mobile/application/social U8 acceptance;
- on an authorized device with an already logged-in application,
  ApplicationRuntime owns the long-lived wait/event/continuation lifecycle and
  RuntimeKernel owns each bounded observe/action/re-observe/verify cycle;
- a specialized external owner is an optional capability, not a prerequisite
  for generic Android operation;
- only when the frozen binding plan actually selects a specialized owner do
  that adapter's current authorization, account scope, receipt, at-most-once,
  and reconciliation requirements apply;
- D20 continues to exclude `F:\dating-copilot` and does not nominate a
  replacement owner;
- one physical owner, current-target/revision validation, fresh post-action
  evidence, no uncertain replay, bounded recovery, durable evidence, candidate
  continuation, restart continuity, and explicit stop/takeover fences remain
  mandatory; and
- at the time of this correction U8 remained `PARTIAL` and U9 was
  `NOT_STARTED`; the required real evidence later passed on 2026-08-24, so U8
  is now `DONE` and U9 is active without changing this route decision.

[constraint-source: USER_DECISION; ref: U8 real mobile/application correction 2026-08-23]

## 2. Current open decisions

No unresolved product choice blocks U9 activation. U8's final accepted plan
selected the generic authorized Android path and no specialized owner; the real
device/application/login, bounded-cycle, delayed-outcome, restart, and stop
evidence is recorded in the U8 work order and current-state document. U9 still
must establish its own production targets and acceptance evidence. A
specialized adapter may not be inferred or selected merely because the target
is a third-party app.

## 3. Decisions that implementation must not invent

- replacing the confirmed baseline visual binding or dynamically switching visual operators without comparative capability and acceptance evidence;
- treating approval for one installation/change plan as blanket authority for materially broader future machine changes;
- defining a real relationship, purchase, account, or other external outcome as completed without admissible confirmation;
- changing the original goal because the generated plan is shorter;
- allowing experience to cross user/account/person/application scope;
- removing legacy data before a verified archive/migration and rollback;
- enabling two physical owners for one target;
- lowering acceptance thresholds after seeing a failed result merely to declare success.

## 4. Decision update format

```text
Decision ID and date
User outcome requested
Chosen behavior in plain language
Technical consequence
Affected documents/work orders
Effective milestone
Evidence or reason
```
