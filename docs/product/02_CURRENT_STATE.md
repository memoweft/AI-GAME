# Current implementation and gap baseline

> Audit date: 2026-08-20  
> Purpose: describe what the repository and inspected local runtime actually prove before vNext work begins  
> Rule: this file must be updated after each completed work order; old phase completion labels are not substituted for current evidence

## 1. Executive assessment

The repository contains valuable, tested pieces of a universal phone agent, including one real natural-language Android execution loop. It is not yet the unified product defined in `01_PRODUCT_SPEC.md`.

Current architecture consists of several orchestration islands:

| Domain | Current role | Can cause physical work | Reached automatically from the primary sentence entry |
|---|---|---:|---:|
| Legacy `MobileTaskRuntime` | General durable Android goal loop | Yes, through ADB | Yes, but only within itself |
| `ApplicationRuntime` | Long-lived fixed-profile cycles; production profile is Soul | Soul through an external owner | No |
| `GameLearner` | Bounded learning episode under an explicit profile | Yes | No |
| `Gateway + RuntimeKernel` | New task contract, state primitives, control, evidence, and leases | Primitive support exists | No autonomous production worker |
| Legacy Chat | Text or text-plus-device compatibility flow | Yes in configured mode | No universal routing |

There is no universal goal router, runtime assembler, automatic capability selection, unified experience memory, or canonical worker that composes these domains.

## 2. What is currently real

### 2.1 MobileTask is the current end-to-end phone loop

`apps/console/backend/ai_game_console/mobile_agent/runtime.py` and `mobile_task_adapter.py` currently provide the closest implementation of the product loop:

- a durable natural-language goal;
- a plan and ordered subgoals;
- local-model planning, decision, verification, and reflection calls;
- current Android screenshot and ADB action;
- post-action screenshot;
- no-progress reflection;
- input revision and stop fences;
- task-level device lease;
- restart handling and no uncertain replay;
- a versioned `SkillMemory` after a claimed successful task.

This path is the migration baseline. It must not be deleted before an equivalent canonical Kernel path has real-device evidence.

### 2.2 RuntimeKernel contains strong primitives but not autonomy

`apps/console/backend/ai_game_console/runtime_kernel/` contains durable Task, Stage, Observation, Action, Verification, Fact, Checkpoint, control, and Lease behavior. It correctly separates transport, verification, and commit.

However, the production gap is decisive:

- the default server does not construct and mount a Gateway composition;
- creating a Gateway task leaves it at `CREATED`;
- no production coordinator repeatedly plans, observes, acts, verifies, and completes it;
- the Gateway composition does not inject a production ADB action executor;
- the Kernel does not yet have the model-role orchestration used by MobileTask;
- historical `kernel_active` drills disabled Legacy writes but did not execute a full natural-language Kernel task.

Therefore the old statement “Runtime Kernel Phase 0-7 complete” means foundation work completed, not that the Kernel owns production autonomous execution.

### 2.3 ApplicationRuntime is a useful long-lived cycle

`application_runtime/runtime.py` already supports a profile-provided observation, policy, execution owner, verifier, memory gate, durable instance, revision fence, wait/wake, pause, stop, and reconciliation cycle.

Current limits:

- one runtime instance is bound to one fixed profile;
- production composition is currently Soul-specific;
- Soul delegates physical truth to the external `dating-copilot` owner;
- it is not selected from the main sentence entry;
- its active memory path is not unified with MobileTask or GameLearning;
- the current Soul policy uses local vision plus a cloud text provider, not the requested local Qwen orchestrator;
- repository documentation states that the new Soul reply chain still lacks current live acceptance.

### 2.4 GameLearning has the strongest immediate transition ledger

`game_learning/` already separates before evidence, proposed action, transport, after evidence, outcome, reward, and versioned `PolicyMemory`.

Current limits:

- users must create a separate LearningJob;
- the only named STZB profile is deliberately narrow;
- positive transitions are retained, while verified wrong actions and recovery paths are not promoted for future avoidance;
- retrieval uses recent action hints instead of current-scene matching;
- scope is profile-level rather than user/app/task/stage/scene/version-level;
- one successful episode can immediately activate a policy revision; there is no separately validated candidate lifecycle in v1.

### 2.5 Current development runtime includes a Qwen3.8 27B multimodal canary

The repository-managed visual asset remains `mPLUG/GUI-Owl-1.5-8B-Instruct`.
Current source now also supports an explicitly selected, machine-local
Qwen3.8 27B multimodal OpenAI-compatible binding. A forced-tool adapter keeps
planning, one-action choice, verification, and reflection as distinct role
contracts even when the same Qwen process serves them sequentially.

This is an early compatibility canary, not the U3 orchestrator. GoalSpecification
success criteria, coverage mapping, independent goal completion verification,
final language result, and verifier-gated promotion are still absent. Qwen Q5 and
GUI-Owl also cannot be resident together on the inspected 24 GiB GPU, so the U3
visual comparison must run serially.

### 2.6 Device and environment setup are only partially automatic

Current code can:

- enumerate ADB targets;
- identify ready/unavailable targets;
- dynamically bind a chosen serial;
- validate configured ADB and model endpoints;
- synchronize a running MuMu instance through an explicit helper;
- show runtime status;
- hold process-level and Kernel leases within their current domains.

Current code does not generally:

- choose among multiple targets based on capability and ownership;
- start MuMu automatically;
- start or repair all required model services from a GoalRun;
- install a missing dependency;
- obtain first-time device authorization;
- select a runtime or profile automatically;
- unify ownership across MobileTask, Kernel, GameLearning, Chat, and external owners.

## 3. Current learning systems

Four mechanisms exist and must be adapted rather than confused:

| Mechanism | Unit learned | Useful property | Missing property |
|---|---|---|---|
| MobileTask SkillMemory | Claimed successful complete task | Durable goal-family hints | No failed action, scene condition, recovery, or candidate validation |
| GameLearning PolicyMemory | Confirmed positive transition | Strong immediate provenance | No negative reuse, weak scene matching, separate job/profile |
| Soul reply learning | Sent reply plus delayed inbound/no-response lineage | Delayed result attribution | Coarse global strategy arms; incomplete production negative/no-response settlement |
| ApplicationRuntime MemoryGate | Profile memory candidate and immutable version | Generic promotion seam | `reward_required` trials have no generic later settlement path |

They use different databases and scopes. No system currently implements:

```text
Goal
-> Episode
-> Scene
-> Action
-> immediate or delayed outcome
-> candidate
-> validation
-> policy revision
-> scene-conditioned retrieval on the next run
```

## 4. Confirmed learning correctness defect

A read-only audit of the local MobileTask database found a completed `auto:stzb/daily-rewards/v1` task whose plan contained only application launch. After that one subgoal passed, the task was marked complete and a daily-task SkillMemory was created, even though the original request included daily rewards/tasks.

Non-secret reproduction evidence from the audited database snapshot:

```text
task_id: 9f22502d-02e4-4342-bfcf-e34d76ecd41d
original_goal: 打开游戏率土之滨，并领取今日奖励，做今日任务
only_subgoal: 点击桌面上的“率土之滨”应用图标以打开游戏
task_terminal_state: completed
skill_scope_id: auto:stzb/daily-rewards/v1
skill_memory_version: 1
skill_memory_source_task_id: 9f22502d-02e4-4342-bfcf-e34d76ecd41d
```

Future tests must freeze these minimal facts into a repository fixture instead of depending on the continued presence of the local runtime database.

Root cause in the current design:

- Planner defines the list of subgoals;
- each subgoal is verified;
- completion of the final generated subgoal immediately completes the task and promotes memory;
- there is no independent final verifier that compares verified results with the original user goal and a frozen coverage checklist.

This is a release-blocking learning invariant for vNext:

> No successful experience or completed GoalRun may be promoted until an independent Goal Completion Verifier establishes that the original goal's success criteria are covered by evidence.

Existing memory must be treated as legacy hints until it passes provenance and goal-coverage validation.

## 5. Evidence currently available

Historical evidence includes:

- a recorded backend suite result of 718 passed and frontend suite result of 52 passed from the prior session;
- a MuMu Android 15 atomic-device smoke that found and repaired two ADB-path issues;
- Lease-to-device-availability tests;
- a local Legacy/Draining/KernelActive/Legacy gate-and-rollback drill;
- one recorded completed STZB MobileTask with 23 ActionAttempts;
- runtime database rows for SkillMemory and GameLearning PolicyMemory.

These facts do **not** prove:

- current full-suite results after future changes;
- a production Kernel task loop;
- a universal sentence router;
- automatic configuration;
- reliable STZB daily completion;
- improved warm-run behavior;
- live Soul autonomous reply learning;
- production deployment.

## 6. Current runtime and repository snapshot

At the audit point:

- the backend mode endpoint reported `legacy`;
- `legacy_writable=true` and `kernel_active=false`;
- Process/User/Machine `AI_GAME_RUNTIME_MODE` were unset;
- the local branch was six commits ahead of `origin/main`;
- source changes from that historical session were committed;
- the user-provided `session.jsonl` remained untracked and is not application source.

This snapshot is time-sensitive and must be rechecked before implementation or deployment.

## 7. vNext earliest real break

The earliest product break is not “add another application profile.” It is:

```text
one GoalRun ingress
-> honest current-runtime binding
-> automatic preflight
-> a real autonomous worker
-> independent goal completion verification
-> evidence-backed positive, negative, and recovery experience
-> measurable warm-run improvement
```

The roadmap deliberately uses the existing MobileTask loop as a compatibility execution binding before attempting a high-risk all-at-once Kernel rewrite.

## 8. U1 confirmed capability — 2026-08-20

U1 is complete at artifact, focused-test, full-backend-regression, and
current-source runtime levels:

- `/api/v2/goals` now accepts an ordinary-language goal plus durable
  idempotency key without device, runtime, profile, model, skill, or owner
  selection fields;
- `GoalRun` and its minimal revisioned `GoalSpecification`, binding, messages,
  controls, and monotonic events are stored additively in
  `runtime/console/goals.db`;
- a configured finite Android goal binds explicitly and at most once to
  `mobile_task_compat`; no configured or ready default target produces
  `WAITING_CONFIGURATION` before any MobileTask side effect;
- compatibility `completed` projects only to non-terminal
  `CANDIDATE_COMPLETE`, with the missing independent U3 verification gate
  exposed in the response;
- v2-bound MobileTasks are durably tagged before execution and cannot promote
  successful SkillMemory, while direct v1 task behavior and historical
  idempotency digests remain compatible;
- U1 exposes only the compatibility control it can enforce: `stop`. Pause,
  resume, and takeover return an explicit capability error;
- the full backend suite passed 724 tests on the final U1 source. The normal
  launcher also proved a missing-target GoalRun, idempotent replay, zero
  MobileTask creation, restart persistence, and event cursor recovery.

Evidence boundary: no default Android target was configured in the inspected
runtime, so current real-device GoalRun completion is `NOT RUN`. U1 does not
claim independent original-goal verification, UI/preflight completion, Qwen
orchestration, universal routing, or real-phone success. Those remain U2/U3
and later milestones.

## 9. U2 partial capability — 2026-08-20 through 2026-08-21

The current source now adds a GoalRun-owned preflight projection and a single
primary product surface:

- the primary frontend creates and reads `/api/v2/goals`; it does not ask for
  runtime, profile, model, skill, or serial selection;
- legacy MobileTask, Gateway, and Soul workspaces remain available only under
  Advanced diagnostics and stable hash deep links;
- preflight records current model, compatibility runtime, ADB discovery,
  Android target, and process-local Lease facts;
- the sole ready idle Android target is selected automatically; configured
  deployment preference is honored; multiple valid targets produce a plain
  selection gate; stale selections are rejected through a fresh assessment;
- non-Android repository targets cannot contaminate phone selection;
- retry continues the same GoalRun after environment changes, and refresh uses
  durable v2 history plus the remembered active GoalRun id;
- GoalRun schema v3 adds a durable repair-attempt ledger. A repair key is
  claimed before mutation, identity uncertainty refuses mutation, restart or
  replay cannot repeat an uncertain `APPLYING` action, and only a post-check
  can produce `VERIFIED`; the projection is returned by the v2 Goal API;
- the final backend suite passed 740 tests after the repair-ledger and
  schema-migration test changes, the frontend suite passed 56 tests, and the
  production frontend build passed.

At the earlier 2026-08-20 snapshot, environment facts were mixed. MuMu 0 was already running and passed
the existing identity/lifecycle checks. Safe synchronization repaired its
loopback ADB configuration and `adb get-state` verified `device`. GUI-Owl was
installed but stopped; its owned startup refused without mutation because only
about 581 MiB GPU memory was free against a 20,500 MiB requirement. The current
GoalRun consequently remained `WAITING_CONFIGURATION` with no bound MobileTask.

U2 remains `PARTIAL`, not DONE. Production managed actions now wrap the existing
MuMu/ADB/model helpers with identity and post-check gates. On 2026-08-21, GoalRun
`a3f35c7b-4ca9-48a3-b866-c4cb5ed812e5` bound MobileTask
`b8e1b87f-6c9e-4b2b-9c04-a9dbec45f651` to MuMu 0 through the current Qwen
multimodal canary. Ten evidence-backed attempts opened Settings, recovered from
its search screen, verified the battery page (74% then 73%, not charging, about
one hour remaining, battery saver off), and verified Home. The MobileTask ended
`completed`, while GoalRun correctly remained `CANDIDATE_COMPLETE`; its
`promote_success_memory` flag was false, memory version remained zero, and no
source memory row was created.

Current-source regression after this change passed 752 backend tests and 56
frontend tests; the production frontend build passed. The normal launcher served
the current API and bundle, and the GoalRun projection persisted. The specified
real primary-composer visual/refresh/close/reopen proof is still `NOT AVAILABLE`:
the Chrome control integration is missing `browser-service.mjs`. The real run was
created through the Goal API rather than proven through the rendered composer.
At that U2 snapshot, formal U3 was not active; the adapter and first real run
were early-canary evidence only. Section 10 records the later explicit advance.

## 10. U3 confirmed capability — 2026-08-21

The user explicitly advanced U3 while retaining U2's unavailable rendered-browser
gate as a separate deviation. Current source now freezes a revisioned, source-
quoted GoalSpecification before device binding, persists append-only completion
assessments, validates every frozen criterion against generated Stage and fresh
verified ActionAttempt references, and permits `COMPLETED` only after that
independent gate.

GoalRun `2a363ad3-5e70-4b99-b25a-5582f5053fbf` froze four settings/battery/report/
Home criteria before binding task `f760003c-1282-4e59-8f8b-54984fb5f943`. Qwen
completed the real MuMu path in five attempts with zero reflection. Independent
completion cited attempts 2-5, verified battery 84%, charging with about one hour
remaining, battery saver off, and final Home, then produced a grounded Chinese
result and terminalized the GoalRun.

The v2 task remained quarantined until this assessment. After verified revision 1
was durable, an idempotent promotion created SkillMemory version 1 with GoalRun id
and completion-revision provenance. The historical launch-only STZB fixture stays
partial and unlearned, and invalid attempt references become uncertain.

On the same five persisted real-device frames, Qwen matched the successful action
path 5/5 in 34.311 seconds; GUI-Owl matched 3/5 in 6.710 seconds and failed finish/
Home decisions. Qwen is the measured current visual default. Both models remain
serial because they cannot coexist on the inspected 24 GiB GPU.

Automated evidence at U3 completion is 757 backend tests and 57 frontend tests,
plus TypeScript and production Vite build. The normal launcher served the current
Qwen composition after restart. The browser-control dependency remains unavailable,
so this does not retroactively satisfy U2's real rendered-window gate.

## 11. U4 confirmed capability — 2026-08-21

Current source adds an independent additive `experience.db` schema-v2 ledger
for ExperienceEpisode, SceneState, ActionTransition, OutcomeSignal,
ExperienceCandidate, PolicyRevision, retrieval, usage, candidate trial and
legacy-import facts. Old MobileTask, SkillMemory and GameLearning databases are
not merged or deleted.

Every v2 MobileTask with frozen criteria opens an experience episode before it
is queued. Finalized attempts record before/after evidence, semantic action,
transport, verifier outcome and recovery lineage. Uncertainty is retained as a
signal and cannot create a candidate. Goal verification is reconciled before
promotion; every promoted candidate must resolve to verified episode scope,
frozen criteria, a real transition, an admissible outcome signal and evidence
references.

Retrieval is bounded by local user/account, application, goal family, UI version,
device/orientation compatibility, current objective and current scene. It uses
standard-library perceptual fingerprints to tolerate fresh evidence ids and
minor frame changes, semantic labels/anchors for explanation, and objective/
target-description similarity for Qwen replans. Current screenshots always
outrank policy; raw coordinates are stored only for audit. Retrieved candidates,
actual positive/recovery use, negative-rule adherence and observed support or
failure are separate durable records.

The controlled three-scene suite covered ordinary, modal and shifted-anchor
states: cold actions 16, warm actions 9 (43.75% reduction), zero false
completion, 100% promoted provenance, zero repeated known-wrong action, 100%
known-wrong recovery, zero intervention and zero scope/UI leakage. Candidate
promotion, rejection, deprecation, confidence update, rollback, stale UI
degradation, restart, legacy SkillMemory validation and selected finalized
GameLearning import are tested.

On MuMu, cold GoalRun `5c3a161d-3608-4aee-aa61-13e2c1e0e418` completed the
settings/battery/report/Home goal in ten actions. Attributable warm GoalRun
`db578fbb-c842-40ea-a998-545c3dda5fe2` completed in five actions, persisted five
retrievals and eight supportive usage/trial records, and did not repeat the known
same-scene Home failures. Independent completion revision 2 verified attempts
2-5 and reported battery 98%, charging with about one hour remaining, battery
saver off, and Home. Revision 1 partial remains in append-only history. A
separate model-outage warm run remains failed and unpromoted evidence.

Final U4 automated evidence is 767 backend tests and 57 frontend tests, plus
standalone TypeScript and production Vite build. The normal console and Qwen
launchers serve current source. This is current-source/local-device evidence,
not a packaged deployment or a production STZB claim. U2's rendered-browser
composer gate remains unavailable and PARTIAL.
