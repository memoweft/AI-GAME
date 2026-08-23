# Acceptance and evidence rules

## 1. Evidence layers

Every delivery report uses these layers separately:

| Level | Question answered | Cannot prove |
|---|---|---|
| A. Artifact | Do source, schema, docs, or assets exist? | That they compile or behave |
| B. Focused automated tests | Does a bounded contract pass controlled tests? | Current full integration or device outcome |
| C. Full regression | Did the repository suite pass at current HEAD? | Real service composition or phone result |
| D. Current-source runtime | Did the normal launcher run current source and expose the expected composition? | A business goal completed |
| E. Atomic device evidence | Did an exact ADB/owner action and observation occur? | A natural-language goal completed |
| F. End-to-end goal | Did one GoalRun meet all frozen success criteria on a real target? | Repeat-run learning or broad competence |
| G. Repeat-run learning | Did comparable warm runs improve using attributable experience? | Another application or production reliability |
| H. External outcome | Did a remote person/service/world outcome occur and get admissibly confirmed? | That AI caused or can guarantee it generally |
| I. Deployment | Is a version installed, observable, recoverable, and serving the intended target? | Long-term business success |

Reports must write `NOT RUN`, `NOT AVAILABLE`, `FAILED`, or `UNVERIFIED` for missing levels. Silence is not a pass.

## 2. Non-negotiable correctness gates

Across every autonomy mode:

- every physical action belongs to one active GoalRun and owner binding;
- current observation, task revision, control state, target identity, and Lease are checked immediately before action;
- transport acceptance never commits Stage or Goal success;
- every important effect uses fresh after evidence;
- only verified facts feed final completion;
- original success criteria are checked independently of the generated plan;
- user stop/takeover prevents new actions;
- uncertain physical actions are reconciled, never blindly replayed;
- repeated no-progress invokes bounded recovery and eventually stops;
- false completion claim count is zero in supported acceptance scenarios;
- policy experience has complete provenance and scope isolation;
- compatibility migration never permits two workers to control one target.

Violation of any item blocks milestone completion even if tests otherwise pass.

## 3. Automated test pyramid

### Domain and schema

- GoalRun lifecycle and legal transitions;
- immutable original goal and revisioned specification;
- idempotency and message/control association;
- binding-plan versioning;
- success-criteria coverage;
- Episode/Scene/Transition/Outcome/Candidate/Policy invariants;
- scope isolation, rollback, and legacy import validation.

### Orchestrator and models

- Qwen JSON/schema conformance;
- no executable action from goal classification;
- GUI-Owl one-action grounding;
- stale observation/revision rejection;
- Planner cannot shrink original criteria silently;
- final verifier rejects the known shortened daily plan;
- model outage/fallback/wait behavior.

### Environment and ownership

- discovery is read-only;
- reversible repair is idempotent;
- sole-target automatic selection;
- multiple-target ambiguity produces one gate;
- external authorization resumes the same GoalRun;
- Lease and external-owner conflicts reject mutation;
- owned service identity checks prevent touching unrelated processes.

### Experience and learning

- positive, negative, uncertain, and recovery transitions;
- uncertain does not become permanent negative memory;
- scene-conditioned retrieval;
- usage attribution;
- UI version degradation;
- candidate validation/promotion/reject/deprecate/rollback;
- cold/warm metrics;
- no cross-user/account/application leakage.

### API and UI

- one goal create/read/message/control/event contract;
- no module/profile/model/serial required for ordinary create;
- frontend state comes from GoalRun projection;
- refresh/reconnect calibrates events;
- advanced diagnostics do not create a second source of truth;
- compatibility history remains readable.

## 4. Real-device scenario A: general phone operation

Goal:

```text
打开设置，查看当前可见的电池信息，告诉我，然后返回桌面。
```

Pass requires one GoalRun evidence package containing:

- original goal and frozen criteria;
- selected device and environment evidence;
- Qwen or compatibility planner stages;
- action-by-action before/after/verification;
- at least one verified battery fact;
- final language result using that fact;
- a final fresh observation proving Home;
- no task-specific fixed coordinates or hidden page script;
- no manual navigation during the ordinary case;
- current source commit, model bindings, device/Android version, and configuration revision.

## 5. Real-device scenario B: STZB daily learning

Goal:

```text
帮我把率土之滨今天的每日任务做完。
```

The GoalSpecification must discover or freeze what “today's daily tasks” means in the current account state. It may return partial or waiting when the environment makes a requirement unavailable; it may not silently reduce the goal to launching the game.

Evidence package:

- initial daily checklist or observable target set;
- each completed/remaining item and evidence;
- action, wrong-scene, recovery, and no-progress transitions;
- experience retrieved and actually used;
- final independent reread/checklist verification;
- cold/warm comparison under comparable conditions;
- application/account/device/version scope;
- interventions and external gates;
- false-completion count.

Acceptance combines resettable fixtures for repeatability and real daily states over time. One day cannot prove a seven-day trend.

## 6. Real-device scenario C: long-lived mobile/application goal

One newly created long-lived GoalRun must demonstrate:

- the same universal sentence ingress and original frozen success criteria;
- automatic discovery of the authorized Android target, target application,
  and current logged-in/readiness state before physical work;
- a durable capability binding plan persisted before any runtime or device
  mutation;
- ApplicationRuntime-owned scheduling, wait/wake, continuation, and bounded
  per-cycle budgets;
- at least one real RuntimeKernel fresh observation followed by one bounded
  application action, a fresh post-action observation, and effect verification;
- an at-most-once physical or external effect with a durable receipt or
  equivalent authoritative evidence;
- immediate action/send truth kept separate from a later inbound message,
  reply, no-response interval, or explicit user-feedback outcome;
- Experience attribution to the exact GoalRun, target/application scope,
  action episode, and delayed outcome;
- candidate/result notification grounded in real application evidence without
  automatically pausing or terminating the active goal;
- at least one later bounded event cycle while the same GoalRun remains active;
- normal-launcher restart restoring the same GoalRun and instance without
  duplicating the prior physical effect; and
- explicit stop fencing later events, wakes, RuntimeKernel actions, and any
  specialized-owner dispatch.

A user follow-up and device-free local checkpoint may prove lifecycle plumbing,
but cannot substitute for the real application event, device action,
post-action evidence, or delayed outcome above. A specialized external owner is
optional for generic Android operation. Only when the frozen binding plan
actually selects such an adapter must the acceptance package also contain that
adapter's current authorization/account scope, readiness, reservation/receipt,
and ledger reconciliation evidence.

[constraint-source: USER_DECISION; ref: D22 U8 real mobile/application correction 2026-08-23]

For social goals, matching and conversation are controllable stages. Another person's relationship decision is not a result the platform can manufacture or guarantee.

## 7. Kernel cutover acceptance

Kernel cutover is not passed by returning `mode=kernel_active`.

Pass requires:

- normal launcher mounts current Gateway/Goal API and autonomous coordinator;
- a real v2 GoalRun completes through Kernel;
- Legacy device writes are blocked;
- pause/resume/cancel/takeover work on the physical path;
- restart recovery and uncertain no-replay are injected and observed;
- snapshot creation and actual restoration are performed on a disposable/controlled copy;
- logs preserve each mode transition;
- rollback to the prior binding is demonstrated;
- no dual owner appears during the entire cutover.

## 8. Learning exit metrics

For supported controlled scenarios:

- false completion: `0`;
- promoted experience with complete provenance: `100%`;
- repeat of evidence-confirmed same-scene wrong action: `<5%`;
- known wrong-scene recovery: `>=90%`;
- warm success rate improves by `>=20` percentage points, or actions decrease by `>=30%` without lower success;
- step-by-step human correction: `0` in supported warm cases;
- cross-scope leakage: `0`;
- regressed policy rollback: demonstrated.

Thresholds may be revised only with recorded evidence and a decision update, not to turn a failing run into a pass retroactively.

## 9. Completion report format

```text
RESULT
What observable product capability changed

ARTIFACTS
Files, schema, and public contracts

AUTOMATED TESTS
Focused and full results at current HEAD

CURRENT-SOURCE RUNTIME
Normal launcher/composition evidence

REAL DEVICE / EXTERNAL OUTCOME
Exact GoalRun and evidence package, or NOT RUN

LEARNING
Cold/warm metrics and provenance, or NOT APPLICABLE

DEPLOYMENT
Installed/cutover/published state, or NOT DONE

ROLLBACK
How the change can be disabled or reverted without deleting user evidence

DEVIATIONS AND OPEN FINDINGS
Every gap against the work order

STATUS
DONE / PARTIAL / BLOCKED / FAILED
```
