# Current implementation and gap baseline

> Audit date: 2026-08-20  
> Purpose: describe what the repository and inspected local runtime actually prove before vNext work begins  
> Rule: this file must be updated after each completed work order; old phase completion labels are not substituted for current evidence

## 1. Executive assessment

The repository contains valuable, tested pieces of a universal phone agent, including one real natural-language Android execution loop. It is not yet the unified product defined in `01_PRODUCT_SPEC.md`.

Current architecture consists of several orchestration islands:

| Domain | Current role | Can cause physical work | Reached automatically from the primary sentence entry |
|---|---|---:|---:|
| Legacy `MobileTaskRuntime` | Read-only migration archive in normal Kernel mode; writable only in explicit Legacy/Draining rollback modes | Yes only in explicit rollback modes | No in normal Kernel-active startup |
| `ApplicationRuntime` | Long-lived fixed-profile cycles; production profile is Soul | Soul through an external owner | No |
| `GameLearner` | Bounded learning episode under an explicit profile | Yes | No |
| `Gateway + RuntimeKernel` | Canonical bounded finite-phone execution path after U7 local cutover | Yes in normal Kernel-active mode | Yes for bounded finite phone goals |
| Legacy Chat | Text or text-plus-device compatibility flow | Yes in configured mode | No universal routing |

There is no universal goal router, runtime assembler, automatic capability selection, unified experience memory, or canonical worker that composes these domains.

## 2. What is currently real

### 2.1 MobileTask is the retained migration baseline and read-only archive

`apps/console/backend/ai_game_console/mobile_agent/runtime.py` and
`mobile_task_adapter.py` provided the migration baseline for the product loop:

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

This path remains preserved for rollback and historical reads. U7 now has
equivalent canonical Kernel real-device evidence for the bounded finite-phone
slice, but no old database is deleted or row-copied.

### 2.2 RuntimeKernel is the normal bounded finite-phone execution path

`apps/console/backend/ai_game_console/runtime_kernel/` contains durable Task, Stage, Observation, Action, Verification, Fact, Checkpoint, control, and Lease behavior. It correctly separates transport, verification, and commit.

U6 added a serial coordinator, existing Qwen role adapter, production Android
observation and typed ADB action ports, GoalRun/Gateway worker wiring, controls,
restart-first observation, independent final completion, and canonical
Experience reuse. U7 promotes that coordinator into the default
`kernel_active` composition with a `runtime_kernel` binding and formal
`KernelTaskAccepted` audit event. The explicit U6 canary switch and its
historical events remain compatible, but normal startup no longer depends on
that switch.

The old statement “Runtime Kernel Phase 0-7 complete” still refers only to
foundation work. U6 was the first current-source full natural-language Kernel
business GoalRun; U7 proves local default ownership and read-only MobileTask
retirement for this slice. It does not prove Soul, multi-device scheduling,
packaged deployment, or the still-unspecified sustained observation period.

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

### 2.4 Legacy GameLearning baseline at the initial audit

At the 2026-08-20 baseline, `game_learning/` separated before evidence,
proposed action, transport, after evidence, outcome, reward, and versioned
`PolicyMemory`.

Legacy GameLearning v1 limits:

- users must create a separate LearningJob;
- the only named STZB profile is deliberately narrow;
- positive transitions are retained, while verified wrong actions and recovery paths are not promoted for future avoidance;
- retrieval uses recent action hints instead of current-scene matching;
- scope is profile-level rather than user/app/task/stage/scene/version-level;
- one successful episode can immediately activate a policy revision; there is no separately validated candidate lifecycle in v1.

### 2.5 Historical pre-U3 Qwen3.8 27B multimodal canary

The repository-managed visual asset remains `mPLUG/GUI-Owl-1.5-8B-Instruct`.
Current source now also supports an explicitly selected, machine-local
Qwen3.8 27B multimodal OpenAI-compatible binding. A forced-tool adapter keeps
planning, one-action choice, verification, and reflection as distinct role
contracts even when the same Qwen process serves them sequentially.

At this pre-U3 snapshot, the binding was an early compatibility canary rather
than the accepted orchestrator. GoalSpecification criteria, coverage mapping,
independent completion verification, final language result, and verifier-gated
promotion were not yet present. Sections 10-14 record their later implementation
and the Kernel cutover. Qwen Q5 and GUI-Owl could not be resident together on
the inspected 24 GiB GPU, so U3 later ran the comparison serially.

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

## 12. U5 continuation capability — 2026-08-21

Current source now rejects false STZB daily identity at both prompt and
deterministic-verdict layers. The map quick strip and the independent task
surface (`名望 / 主要事宜 / 事务`) are not a complete daily checklist. Negative
confirmation of that fact is allowed, while a positive daily-list verdict must
cite a visible daily/today/activity label. Semantic action targets and recent
verifier evidence are retained in role history; malformed forced-tool responses
have bounded repair; reflection retains the unfinished plan tail and can return
from an exhausted task surface to main navigation.

The STZB daily family now freezes a deterministic three-criterion specification
without depending on a free-form model response: complete current-day checklist
discovery, per-item completed-or-blocked evidence, and an independent reread of
the same list. Goal list/detail reads no longer invoke checklist vision on
failed/stopped/uncertain records; the ten-goal list returned in 158 ms on the
restarted current source. Experience orientation is `unknown` until the first
real frame rather than falsely defaulting to portrait.

Real MuMu evidence reached `精彩活动` and visibly identified two daily-labelled
cards: `登录奖励` with `每天登录领取丰厚奖励`, and `心愿征程` with
`每日招募可获额外心愿积分`. This establishes a real semantic route and explicit
daily identity only; it is not evidence of complete checklist coverage.
GoalRun `c2189755-ce26-4c9f-95af-6626e7159ce5` then produced four verified
actions that returned to main navigation and rejected the chapter, main-affairs,
and affairs pages without executing their occupation/warehouse tasks. Its
recovery ended `FAILED` on model unavailability with no uncertain replay. A
second GoalRun `853eb57a-c295-4681-a147-c7b74efd7ac9` failed during planning
after 180 seconds and performed zero actions.

The machine-local Qwen3.8-27B launcher explicitly disables prompt caching and
uses a 512-token vision budget. One persisted-frame verification correctly read
both daily cards in 40.96 seconds; the next identical request timed out at
180.04 seconds, and non-consecutive-token warnings remain. Therefore the visual
runtime stability gate is still red. U5 remains `PARTIAL`: no complete daily
checklist has been frozen, no feasible checklist has been completed and
independently reread, no real comparable cold/warm pair exists, and there is no
deployment or external-outcome evidence. Focused verification passed 53 tests;
the final current-source regression passed 786 backend tests and 57 frontend
tests, plus TypeScript and the production Vite build. U6 is not permitted to
start.

### 12.1 Later U5 stabilization and discovery evidence

The later U5 continuation reduced the machine-local Qwen context from 65,536
to 32,768 tokens while retaining flash attention, quantized KV cache, and
1,024-pixel vision input. The same persisted dense MuMu frame then completed
three consecutive correct reads in 11.96, 11.37, and 11.54 seconds; the earlier
65,536-token configuration had timed out at 240 seconds with only about 260 MiB
of free GPU memory. This closes the narrow repeated-frame canary break, not the
whole end-to-end role/runtime gate. The server still emits non-consecutive-token
warnings, and a reflection with thinking enabled took about 30 seconds before
the current source disabled reflection thinking and bounded it to 512 tokens.

Current source now uses PNG perceptual hashing to reject animation-only frame
changes (real same-page distances were 0-7; actual dialog/page changes were
22-27). Discovery-only STZB goals freeze a two-criterion contract—bounded
checklist discovery plus preservation of every item without execution—while
ordinary completion goals keep the execute-and-reread contract. Checklist
schema revision 2 can combine bounded views of the same surface and daily cycle
only after start and end coverage are both evidenced. It still requires an
independent matching manifest before a normal completion claim.

Real MuMu discovery GoalRun `768ade2f-7b61-4b06-8f52-4cbf3bb4a07a` navigated
the task surface and activity carousel using only taps, swipes, close actions,
and read-only detail views. It confirmed `事务 0/15 / 暂无事务`, rejected the
chapter and reputation surfaces as daily lists, opened `登录奖励 / 俸禄` and
read all 14 cumulative-login entries with three completed days, and opened
`心愿征程` with activity dates `2026-06-24` through `2026-11-04`, the rule that
the first four daily faction-general recruits grant 35 extra wish points, and
today's `0/4` progress. It did not press `前往招募`, claim, complete, or other
item-execution controls. A final directed run stopped `UNCERTAIN` after one
accepted tap produced no material visual change; the action was not replayed.

These facts prove current-source navigation, daily-candidate identity,
perceptual no-progress recovery, discovery-only non-execution, and uncertain
stop behavior on the real device. They do not prove complete carousel and
entry coverage: the durable checklist remains `NOT_DISCOVERED`, no manifest is
frozen, no item execution or independent reread occurred, and no comparable
real cold/warm pair exists. The final backend regression for this continuation
passed 794 tests. At that snapshot U5 remained `PARTIAL` and U6 was disallowed;
D16 later removed only the scheduling block without closing these U5 gaps.

### 12.2 Open-development execution and recovery evidence

Under the owner's explicit open-development test directive, ordinary STZB
completion goals no longer inherit discovery-only non-execution behavior. The
runtime plan and real-device attempts included recruit, occupation, warehouse
upgrade, reward claim, patrol, and reread stages. Generic invariants remained:
one device owner, user stop, revision fences, validated actions, post-action
observation, no uncertain replay, bounded reflection, and durable evidence.

Current source now normalizes Qwen's exact `tap` alias to the shared `click`
action, requires separately verifiable task tabs and boundaries, retries an
invalid 1-16-stage semantic plan once, counts an unsatisfied `finish` as no
progress, recognizes proven negative daily identity, and accepts explicit
daily mechanics in an activity detail body without treating a limited event
name as disqualifying. Initial and reflection-generated recovery plans both
reject a presumed daily identity for `任务面板`, `“任务”总览`, `任务入口`,
`主要事宜`, `事务`, and `名望`; Chinese/English quotes and whitespace between
`任务` and the surface name are normalized. Visible tutorial prerequisites and
multi-page `获得新战法` reward presentations receive explicit executor handling.

Real MuMu runs separately reread `主要事宜`, `事务 0/15 / 暂无事务`, and
`名望`; all three correctly closed as having no visible daily/today reset
identity. They covered both visible boundaries of the active `精彩活动` carousel
and opened multiple independent details. `集思问策` explicitly stated its
daily 12:00, 16:00, and 20:00 answer sessions, five questions per round, 1,000
coins per correct answer, and at most five daily rewards; at the observed time
the next session was unavailable until 20:00. `赴汤蹈火` was independently
recorded as a dated collaboration activity with no visible daily mechanism.

The same open-development runs performed three real reward claims from
`七日试炼`: cumulative login days 1, 2, and 3. The device displayed successive
`获得新战法` reward pages; later reread showed the day-2 100-point node green and
current score 5, and the day-3 return showed current score 6. These are real
action/result facts, not proof that all daily tasks completed. Patrol reached
`巡察次数 5/5 / 当前事件 6`, opened an event and its prerequisite rules tutorial,
but did not verify event completion. Recruit, occupation, and warehouse upgrade
were planned and attempted as reachable stages but did not reach a verified
result before the runs stopped.

GoalRun `a1c927c1-6749-40a4-af83-346df74ad587` stopped `UNCERTAIN` after an
accepted activity-card tap was verified only against a black loading frame;
the action was not replayed. A later read-only frame showed the loaded
`赴汤蹈火` detail, and continuation GoalRun
`2b18f8de-bb6b-4bb8-b73e-46f2c0358cba` resumed from that observed state rather
than repeating the accepted tap. The continuation was stopped after a quoted
`“任务”总览` wording variant exposed a recovery-plan identity loophole; current
source closes that loophole.

The final current-source backend regression passed 800 tests. Focused mobile,
tool-role, and STZB verification passed 53 tests. U5 remains `PARTIAL`: no
durable complete manifest was frozen, patrol/recruit/occupation/upgrade were
not all verified, no independent end-to-end final reread completed, and no
comparable cold/warm pair or deployment/external-outcome evidence exists. The
owner-authorized pre-manifest action test is a recorded development deviation,
not satisfaction of the roadmap acceptance order. At that snapshot U6 was
disallowed; D16 later removed only the scheduling block.

A subsequent real-device continuation closed several earlier action-evidence
gaps without closing U5. One recruit was actually executed; the later visible
new-general tutorial named `荀彧`, corroborating that the recruit produced a
new general. The warehouse visibly advanced past the requested target to
`3/20`, while the device toast `仓库Lv.2建造完成` and the completed chapter row
prove the Lv.2 task outcome. The extra warehouse level is a recorded deviation:
the small level change was initially classified as no material visual change,
so one additional upgrade tap was sent before the owner update fenced further
upgrades.

Occupation did not complete. The runtime selected a `土地Lv.2` surface and sent
one confirmed sweep, but the target later showed an `弃` countdown and the
independent `主要事宜` reread remained `占领Lv.2土地 1/4`. The earlier inference
that the selected tile was a non-owned occupiable target was therefore false,
and no `2/4` claim is made. Patrol did execute on the real device: successive
fresh patrol pages changed from `巡察次数 4/5` to `3/5` and then `2/5`; the final
page showed `当前事件 24`. The second decrement occurred after an accepted tap
whose immediate map-only verification could not establish its result, so that
task correctly stopped `UNCERTAIN` and did not replay the tap. A new
observation established the `2/5` state, and a read-only continuation completed
with no further patrol action and no visible newly claimable reward.

This continuation also exposed runtime limitations. Two GoalRuns ended
`mobile_role_unavailable` after local Qwen requests timed out, the model service
was recovered through its owned restart script, and a later STZB GoalRun was
rejected by the semantic plan gate rather than weakening task-page identity
rules. A narrow compatibility MobileTask then completed the patrol final reread.
Accordingly, these are real current-source device results, but not a complete
universal GoalRun acceptance: the durable manifest, valid occupation, complete
reward closure, independent whole-set reread, cold/warm comparison, learning
proof, deployment, and external outcome remain open.

Current source now adds a deterministic STZB numeric-verdict guard: when a
subgoal requests an explicit ratio such as `2/4` but fresh evidence explicitly
shows a different current ratio such as `1/4`, or states that the target was
not reached, a model `satisfied` verdict is downgraded and cannot advance the
plan. Matching ratios still pass. The focused mobile/tool-role/STZB regression
passed 56 tests, and the full backend regression passed 803 tests with the one
pre-existing Starlette/httpx deprecation warning.

### 12.3 Takeover planning and recovery evidence

The latest U5 continuation tightened the ordinary full-goal planning gate. A
single identity check, a combined all-tabs discovery stage, or a plan without
both executable-or-blocked item handling and an independent final reread is no
longer accepted as a complete STZB plan. Planning receives at most three
bounded semantic attempts with issue-specific feedback. Recovery stages remain
smaller insertions because the runtime preserves the unfinished plan tail.

A real failure exposed one validator false positive: a recovery result that
explicitly closed or exited the generic task panel and then sought an
independent daily/activity/patrol surface was rejected merely because the same
sentence contained both `任务面板` and `每日`. Current source permits that explicit
surface separation while continuing to reject claims that `主要事宜`, `事务`,
`名望`, or the task panel itself already has daily identity. The failed task's
same persisted context then produced an accepted Qwen recovery decision in
11.30 seconds, and focused mobile/STZB/Qwen tests passed 52 cases.

Ordinary GoalRun `88f9dc24-a937-4899-98f0-d3697554bb92`, bound to MobileTask
`df199be3-dddc-4cfb-a8ff-ec6f810d998e`, persisted a full plan and used the real
MuMu target. It correctly read `主要事宜` and `事务 0/15 / 暂无事务` without
granting them daily identity, but its second reflection ended
`mobile_role_invalid_response`. No uncertain physical action was replayed and
the device lease was released.

After the validator fix, GoalRun `bcb957be-1352-4a68-be75-c64ca4a4d1a2`, bound
to MobileTask `ba7fe121-9451-461c-80a8-8e4477672a54`, crossed that exact second
reflection boundary. It recorded five bounded reflections and 41 attempts,
independently closed the generic task tabs, traversed the activity carousel,
and visibly identified `登录奖励 / 每天登录领取丰厚奖励` and
`心愿征程 / 每日招募可获额外心愿积分`. It still did not freeze a complete
manifest or execute and independently reread the frozen set. The sixth proposed
recovery contained a conditional `若...若没有...` branch; its alternate reply
again presumed a daily page inside the generic task panel. Both violated the
existing recovery contract, so the run correctly ended `FAILED` with
`mobile_role_invalid_response` instead of weakening the identity gate or
claiming completion.

The final current-source backend regression passed 805 tests with the one
pre-existing Starlette/httpx deprecation warning. This is source, automated,
and real local-device evidence only. Learning improvement, a comparable real
cold/warm pair, packaged deployment, and external outcome were not established.
At that snapshot U5 remained `PARTIAL` and U6 was disallowed; D16 later removed
only the scheduling block.

### 12.4 Physical discovery, manifest-driven planning, and detail evidence

The 2026-08-23 U5 continuation moved activity discovery from model-declared
carousel positions to physical transition evidence. A start or end boundary is
accepted only after a correctly directed swipe produces no material visual
change; a changed frame proves traversal progress and cannot simultaneously
prove a boundary. The middle sample likewise requires a material transition,
and its executor instruction targets a visible activity card instead of blank
background. These rules are capability-based and contain no fixed coordinates
or activity-title macro.

The STZB progress controller now normalizes all activity views in one GoalRun
to `current-daily-cycle`, filters candidates by positive daily/today mechanics,
canonicalizes their titles, and replaces generic detail placeholders as soon
as the candidate manifest is available. Each candidate is handled by two
separately verifiable stages: locate the exact card, then open and read the
exact detail. The same manifest drives the later execution and final-reread
plan expansions. These STZB rules are bound only to the STZB daily goal family;
the shared runtime seam remains a neutral progress-plan controller contract.

Real GoalRun `2d427996-af07-4443-ba9b-d2289668cce2` proved both physical
activity boundaries and a materially distinct middle view on MuMu. It produced
exactly two positively identified daily candidates: `心愿征程` and `登录奖励`.
GoalRun `29a589f3-83f2-411c-a2f5-b6973e05ec65` then proved the immediate
revision-fenced replacement from generic placeholders to those exact titles;
it ended before a physical detail action after three invalid model tool-call
responses. GoalRun `1cbecd50-6510-4af6-bb80-d0a8a743919e` located both exact
cards, opened `心愿征程`, recorded its first-four-daily-recruits mechanic and
visible `0/4` state, and opened the `登录奖励` detail showing days 1 through 14,
five accumulated login days, and the first five checked entries.

The last run was stopped safely after the older numbered-login guard rejected
the newly generated exact-candidate subgoal wording. Current source extends
that guard to the manifest-derived `登录奖励` wording, but this last extension
has automated evidence only and has not been rerun on the device. The detail
snapshot and candidate-specific coverage were nevertheless persisted before
the verdict downgrade. All directed runs were stopped or cancelled without
pre-freeze daily-item execution. No complete multi-surface manifest was frozen:
the same run still lacks task-major, task-affairs, task-reputation, and patrol
coverage.

Focused STZB/mobile/runtime verification passed 108 tests. The complete backend
regression passed 837 tests with one pre-existing Starlette/httpx deprecation
warning. No whole-set execution, independent final reread, comparable cold/warm
pair, attributable learning improvement, deployment, or external outcome was
established. At that snapshot U5 remained `PARTIAL` and U6 was disallowed; D16
later removed only the scheduling block.

## 13. U6 confirmed Kernel canary capability — 2026-08-23

At the U6 closeout snapshot, current source had a distinct
`KernelCanaryCoordinator` over the durable
RuntimeKernel. With `AI_GAME_KERNEL_CANARY_ENABLED=1`, v2 GoalRun binds once to
`runtime_kernel_canary`; the default/unset configuration still binds to
`mobile_task_compat`. The same coordinator is attached to Gateway task creation,
so enabled Gateway tasks are submitted to a real serial worker instead of
remaining `CREATED`. U7 default cutover and Legacy write retirement are not part
of that U6 switch; section 14 records their later promotion.

The coordinator uses the existing Qwen plan/decision/verification/reflection
port, production Android observation provider, typed ADB action executor,
Kernel SQLite execution Lease, and process-level device lease. It records every
plan Stage, fresh Observation, proposed Action, transport result, after
Observation, role verdict, Stage completion, Checkpoint, candidate completion,
and final Task completion in the Kernel stream. Pause is fenced against proposal
and dispatch; resume starts a new worker turn and observes before deciding.
Cancel and takeover use the same durable Kernel controls. An accepted action
that is still unverified at restart becomes `TaskUncertain` without replay; the
frozen Task status stays `FAILED` and the uncertainty is explicit in
`failure_state.last_verdict=UNCERTAIN`.

Real MuMu GoalRun `ed42b714-4106-4332-b134-b3d8a1b8e6be`, bound to Kernel Task
`2bc95172-d1dd-4959-b513-a05f690736ca`, froze five settings/battery/Home
criteria. It completed three Stages in four actions: opened Settings, scrolled
until the Battery row was visible, opened the Battery detail showing 95%, and
sent Home. The three inspected after frames visibly show the Settings main
screen, Battery detail, and the launcher desktop. Before the first action, the
owner pause produced `PAUSED`; after three seconds both total events and
`ActionProposed` count were unchanged. Resume recorded a new observation before
the first proposal. All four actions had fresh after observations and role
verdicts. Goal Completion independently cited attempts 1, 3, and 4 for all five
criteria, committed `COMPLETED / verified`, and only then caused Kernel
`TaskCompleted`. Runtime mode reported zero active Legacy tasks before this
canary.

The Kernel canary now writes to the U4 canonical Experience ledger. Current-
source desktop GoalRun `01a7a497-5854-405a-9512-07653a390f23` created an Episode,
recorded one verified transition, and promoted candidate
`665aafd5-6f8c-40f9-a1e6-eecf3a2e8e13` only after final Goal verification. The
next same-scene GoalRun `86ca39b9-823d-4197-895f-b3e4ea36fb68` retrieved that
exact active candidate before the decision; the hint entered
`DecisionContext.experience_hints`, but fresh observation and all verification
gates still ran. Both the second GoalRun and Kernel Task completed.

Final automated evidence is 844 backend tests with one pre-existing
Starlette/httpx deprecation warning, 57 frontend tests, and a passing TypeScript
plus production Vite build. The accepted-unverified restart test proves one
failure injection without a second Action. The normal launcher served the final
current source with explicit canary composition. These facts close U6, but they
do not prove packaged deployment, default Kernel cutover, browser-window
acceptance, Soul, multi-device scheduling, external outcome, or U5's missing
complete STZB manifest and cold/warm acceptance.

## 14. U7 local real-Kernel cutover — DONE — 2026-08-23

The normal console launcher now defaults to `kernel_active`. Current-source
startup constructs the real Gateway, RuntimeKernel, production-mode
coordinator, configured Qwen role, ADB observation/action adapters, v2 Goal
service, and canonical Experience service; it fails closed if that composition
cannot be constructed. Legacy device-write routes are fenced, old MobileTask
history is available through an explicit read-only compatibility namespace,
and Legacy workers are not started in active mode. The final default start
reported `kernel_active`, `legacy_writable=false`, and zero active Legacy tasks.

The real local sequence drained Legacy to zero, created a SQLite snapshot,
restored it into a distinct controlled copy, activated Kernel, exercised a
guarded rollback to writable Legacy, and repeated drain/snapshot/restore before
the final Kernel activation. The final source and restored copies both passed
SQLite integrity and had logical SHA-256
`daf0c8935eec5892e46d4cdf98ff7b0584faf07cf7e21ec6f0be39e8f67f1b71`.
Mode/operator logs are append-only, and console stdout/stderr from previous
starts is archived rather than truncated.

Real MuMu GoalRun `aeddc7a8-9f8f-4f1d-8dd5-f1bbcafbfc09`, bound to Kernel Task
`1e98f1f1-6671-4d88-a7b4-2c4f9cb62471`, held stable during owner pause, resumed,
and completed two verified actions. Independent completion verified Settings,
visible Battery data (78%, charging with about one hour remaining, Battery
Saver off), reporting that data, and returning Home. GoalRun
`a6f6c79e-957c-4627-9016-a060d75784bf` proved pause/stop with durable
`CANCELLED` and zero proposed actions. A default-start current-source task then
recorded the production `KernelTaskAccepted` event and was pause/cancelled with
zero actions. Restart preserved all terminal event/action counts. The separate
accepted-unverified failure-injection test marked the task uncertain without
replay.

Final automation passed 849 backend tests (one pre-existing Starlette/httpx
deprecation warning), 57 frontend tests, TypeScript checking, and the Vite
production build. These are automated/source facts; the completed GoalRun and
cutover/rollback sequence are current-source local runtime and real-device
facts. They do not prove packaged deployment, a sustained production
observation period, external outcome, rendered-browser acceptance, or U5's
remaining STZB learning gates.

The final D17 observation ran from `2026-08-23T06:45:11.0180213Z` through
`2026-08-23T07:15:12.5140444Z`: 1,801.493 seconds and 61/61 successful
30-second samples. Every sample reported healthy console/database, ready
runtime, `kernel_active`, Legacy read-only, zero active Legacy/Kernel tasks,
and zero active Leases. The midpoint controlled restart completed in 3.675
seconds and changed the listener from PID 35380 to 22484. Runtime-mode logs
appended one stop/composition/start sequence with `runtime_kernel` and Legacy
workers disabled. Legacy writes returned
`403 / LEGACY_DEVICE_WRITE_DISABLED` before and after restart; the Legacy
archive, Kernel task count (7), and GoalRun count (86) remained readable and
stable. No fatal/unhandled console error was found. Evidence is stored in
`runtime/logs/u7-observation-20260823T0645110180213Z.jsonl`, SHA-256
`AE346166DA7FD6A386C5A27F6710C60811DD91FBD5AE4D8BCE1BD52181E01B8D`.

Current-source closeout verification passed 98 focused U7/Kernel/control tests,
849 complete backend tests with one pre-existing Starlette/httpx deprecation
warning, 57 frontend tests, TypeScript checking, and the Vite production build.
These facts close U7 and activate U8 under the ordinary advancement rule.
Packaged or production deployment remains U9 `NOT RUN`.

The live role endpoint also reported `qwen3.8-27b-u` with the current external
65,536-token configuration rather than the earlier documented
`qwen3.8-27b`/32,768 snapshot. U7 did not change that machine-local binding and
the real goal plus the observation passed, but without a controlled comparison
it is runtime drift, not an accepted replacement baseline.
