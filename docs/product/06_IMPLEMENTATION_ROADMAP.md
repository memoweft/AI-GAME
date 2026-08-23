# Universal Agent implementation roadmap

## 1. Roadmap identity

This is the **Universal Agent Track**, identified by `U0` through `U9`. It does not reuse either historical Phase 0-7 numbering scheme.

The target is delivered through vertical slices. Existing MobileTask remains the first compatibility execution engine; the platform does not begin with an all-at-once merge of MobileTask, RuntimeKernel, ApplicationRuntime, GameLearning, and Soul.

## 2. Ordering principles

1. Establish one honest product goal object before hiding current modules.
2. Prove the current real device loop through the new facade before rewriting it.
3. Fix false goal completion before promoting new learning.
4. Prove repeat-run improvement on the current executable path before migrating that behavior to Kernel.
5. Make Kernel execute one full natural-language business goal before runtime cutover.
6. Cut over device execution before adding broad multi-capability routing.
7. Route long-lived application capabilities only after at least two execution types have real acceptance.
8. Add production policy and deployment hardening after the open development loop works.

## 3. Milestones

### U0 — Canonical product specification

**Outcome**: one current product definition, current-state audit, target architecture, learning contract, roadmap, evidence rules, AI protocol, and work-order set.

**Status**: `DONE` — canonical specification reviewed; open product decisions are recorded and no product code behavior changed.

**Exit evidence**:

- old frozen design package removed or superseded;
- root `AGENTS.md` points future AI to this package;
- old Kernel roadmap is labeled historical;
- current capability and false-completion defect are documented;
- no product code behavior changed.

### U1 — Universal Goal contract and compatibility facade

**Outcome**: one durable GoalRun API accepts ordinary language without requiring device, runtime, profile, model, or skill selection. Supported phone goals bind explicitly to the current MobileTask engine.

**Status**: `DONE` — additive v2 facade, compatibility binding, honest
completion projection, restart/idempotency, and v2 success-memory quarantine
passed the U1 automated and current-source runtime gates on 2026-08-20. A real
phone goal was not run because no default target was configured; U2 owns the
formal product-surface real-device gate.

**Includes**:

- GoalRun/GoalSpecification minimal persistence;
- idempotent create/read/message/control/event contract;
- compatibility binding record to a MobileTask id;
- honest state projection and error mapping;
- transitional target rule: use only an already configured default target; otherwise wait explicitly for configuration until U2 adds discovery/selection;
- quarantine or suppress successful SkillMemory promotion for v2-bound compatibility tasks until independent original-goal verification exists;
- no automatic multi-runtime classification yet;
- API and migration tests.

**Real proof**: with an already configured default target, create a settings/battery goal through `/api/v2/goals`, observe the same compatibility MobileTask, and retrieve one coherent GoalRun projection. If the underlying generated plan ends, GoalRun remains `CANDIDATE_COMPLETE` and no successful goal memory is promoted.

**Rollback**: disable/remove only the v2 facade; v1 MobileTask history and execution remain intact.

### U2 — One product surface and automatic preflight

**Outcome**: the primary UI contains one goal composer. A GoalRun discovers model/device readiness, selects the sole compatible idle target, applies safe reversible configuration, or shows one plain external gate.

**Status**: `PARTIAL` — one composer, durable preflight facts, target/Lease
assessment, selection gates, same-GoalRun retry, compatibility diagnostics,
managed production repairs, automated regressions, production build, and a real
Qwen-backed settings/battery compatibility GoalRun now exist. The run remains
honestly `CANDIDATE_COMPLETE` and promoted no success memory. U2 still lacks the
specified real primary-composer visual/reopen proof because browser control is
unavailable in the inspected environment.

**Includes**:

- GoalRun preflight states;
- capability/environment projection;
- automatic sole-target selection;
- MuMu/ADB/model helpers exposed behind managed checks;
- device and settings moved to supporting/advanced views;
- Soul/Gateway/Learning/legacy workspaces retained as hidden compatibility diagnostics;
- no database cutover.

**Real proof**: from the primary composer, run the settings/battery task without selecting a module, profile, model, or serial. Refresh and frontend closure must not lose the task. U2 proves surface, preflight, binding, and device progress; it must retain `CANDIDATE_COMPLETE` rather than claim formal goal completion before U3.

### U3 — Local Qwen goal orchestration and goal-completion gate

**Outcome**: the configured local Qwen3.8 27B binding owns goal interpretation, success criteria, Stage planning, replanning, reflection, memory reasoning, and user-language result. The current tuning baseline also binds its proven image input to visual action roles; U3 still selects the accepted visual default through a serial comparative acceptance against GUI-Owl.

**Includes**:

- model capability registry and health;
- Qwen structured adapter;
- comparable GUI-Owl versus visual-Qwen evaluation when Qwen image input is actually supported;
- immutable original-goal plus revisioned GoalSpecification;
- Planner output coverage mapping;
- independent Goal Completion Verifier;
- no successful Skill/Experience promotion without goal-coverage evidence;
- current MobileTask integration through adapters, not a full Kernel migration.

**Release blocker**: the known “launch game equals daily tasks complete” case must become partial/failed, never completed or learned.

**Real proof**: the settings/battery GoalRun from U2 passes the full scenario-A completion gate in `07_ACCEPTANCE_AND_EVIDENCE.md`, while the shortened STZB plan remains unverified and unlearned.

**Status**: `DONE` — explicitly advanced by the user while the unrelated U2
rendered-browser gate remains recorded as `PARTIAL`. Current Qwen freezes source-
quoted criteria before binding, independent completion validates Stage and
ActionAttempt references, verified completion gates successful memory, and the
fresh settings/battery GoalRun completed on MuMu with five attempts. The
historical launch-only STZB fixture remains partial and unlearned. A same-frame
serial comparison selected Qwen over GUI-Owl for visual correctness.

### U4 — Unified experience ledger and scene-conditioned memory

**Outcome**: phone actions create a canonical Episode/Scene/Transition/Outcome ledger; verified positive, negative, and recovery experience can become reversible candidates and policy revisions.

**Includes**:

- additive experience database/schema;
- adapters from MobileTask ActionAttempts and selected GameLearning facts;
- semantic scene representation;
- wrong-scene, no-progress, and recovery experience;
- candidate validation, confidence, scope, compatibility, promotion, deprecation, and rollback;
- retrieval-use attribution;
- legacy SkillMemory treated as untrusted hint until validated;
- cold/warm fixture benchmarks.

**Does not include**: model-weight training or forced merge/deletion of old databases.

**Status**: `DONE` — additive schema-v2 experience storage, compatibility
adapters, perceptual/semantic scene and objective retrieval, usage attribution,
candidate/policy lifecycle and rollback passed controlled gates. A comparable
real settings/battery cold run used ten actions and the attributable warm run
used five while both verified complete.

### U5 — STZB daily repeat-run learning vertical

**Outcome**: the sentence “帮我把率土之滨今天的每日任务做完” uses the universal GoalRun, open development autonomy, real screen evidence, recovery, and cross-run experience.

**Includes**:

- dynamic daily checklist discovery;
- no tutorial-profile keyword blacklist in the universal path;
- scene/action/outcome/recovery capture;
- independent final reread of the daily checklist;
- comparable cold/warm trials;
- application-version and account-scope isolation;
- metrics in `04_AUTONOMY_AND_LEARNING.md`.

**Real proof**:

- controlled repeatable fixtures for rapid development;
- real authorized MuMu/device runs over actual daily states;
- no step-by-step correction in supported warm scenarios;
- zero false “all daily tasks completed” claims.

Passing U5 proves one learning vertical, not general game mastery.

**Status**: `PARTIAL` — the family binding, dynamic checklist gate, controlled
fixtures, current-source runtime, and real MuMu attempts exist. A real run
now separates the chapter/reputation/affairs pages from explicit daily identity,
covered visible activity-carousel boundaries, and reached real daily-labelled
activity details. Under the owner's explicit open-development test override,
later runs also claimed three visible cumulative-login rewards and verified the
resulting reward flow without claiming overall completion. The narrow repeated dense-frame Qwen canary is now stable at a
32,768-token context (three correct reads in 11.96, 11.37, and 11.54 seconds),
and later device continuations added verified recruit, warehouse-Lv.2, and
patrol-count changes. Occupation still remained `1/4`; the selected sweep target
proved invalid for occupation, and the final patrol closure required a narrow
compatibility MobileTask after GoalRun/model failures. No durable complete
multi-surface manifest, whole-set independent reread, comparable cold/warm
pair, or learned warm-run improvement exists. The pre-manifest actions are
development evidence, not acceptance of the roadmap ordering. A deterministic
ratio guard now prevents a requested `2/4` state from passing against visible
`1/4` evidence. The latest takeover continuation also rejects shortened
full-goal plans, accepts recovery that explicitly leaves a generic task panel
for an independent daily surface, and crossed two previously failing real
reflection boundaries. Its later conditional/presumed-identity recovery was
correctly rejected; no complete manifest was frozen. The resulting
current-source backend regression is 805 passed.
The 2026-08-23 continuation additionally replaced model-declared carousel
boundaries with correctly directed terminal-swipe evidence, normalized one
run's activity views to a single current-cycle identity, and expanded generic
detail stages from the discovered candidate manifest. Real MuMu runs proved
both activity boundaries, a distinct middle view, an exact two-candidate set
(`心愿征程` and `登录奖励`), revision-fenced candidate-specific planning, both
candidate-card locations, and both detail pages. The last exact-login-detail
verdict extension is automated-only and still needs a fresh device run. No
complete multi-surface manifest was frozen and no post-freeze execution or
independent final reread occurred. Focused verification is 108 passed and the
full current-source backend regression is 837 passed with one pre-existing
deprecation warning.
U5 remains `PARTIAL`, but it is no longer a roadmap-advancement blocker. On
2026-08-23 the owner explicitly directed the team to close U5 quickly, reduce
its blocking documentation constraints, and execute U6. The missing complete
manifest, whole-set execution/reread, and comparable cold/warm evidence remain
truthful U5 stabilization gaps and still block a U5 `DONE` or production STZB
claim; they do not block the bounded, reversible U6 settings/battery canary.

### U6 — Canonical Kernel autonomous worker canary

**Outcome**: Gateway/RuntimeKernel can independently complete one full natural-language settings/battery GoalRun using the same model, device, verification, experience, and control semantics.

**Includes**:

- role ports and model router;
- coordinator/worker;
- production ADB action executor;
- task-session ownership;
- plan/observe/act/verify/recover/final-verify loop;
- pause/resume/cancel/takeover;
- restart-first observation and no uncertain replay;
- v2 GoalRun canary binding.

**Not included**: Soul migration, broad router, or simultaneous dual execution.

**Rollback**: switch v2 binding back to the MobileTask compatibility engine; never dual-run a goal.

### U7 — Real Kernel cutover and compatibility retirement

**Current status**: `DONE` on 2026-08-23. The local normal-launcher cutover,
real GoalRun, controls, restart recovery, actual SQLite restore, append-only
logs, explicit Legacy archive, and guarded rollback passed. The final D17
observation then passed for 1,801.493 seconds with 61/61 healthy samples, a
3.675-second controlled restart, zero active tasks/Leases, intact logs, and
Legacy write rejection before and after restart. Packaged or production
deployment acceptance remains U9 scope.

**Outcome**: default launcher in Kernel mode constructs the real Gateway, coordinator, model bindings, action executor, experience service, and v2 Goal API. Legacy device writes are disabled only after Kernel completes a real business goal through the normal product entry.

**Required sequence**:

```text
legacy
-> draining
-> zero in-flight legacy work
-> snapshot
-> kernel_active with real worker
-> create and complete a real GoalRun
-> observation period
-> accept or restore
```

**Required proof**:

- default startup, not a test-only composition;
- real goal, not atomic ADB smoke;
- actual pause/resume/cancel behavior;
- event/state recovery;
- an actual snapshot restoration exercise;
- durable non-truncating mode logs;
- explicit legacy read-only archive.

### U8 — Multi-capability and long-lived goal routing

**Status**: `IN_PROGRESS` — activated after U7 completed its ordinary
advancement gates on 2026-08-23. No U8 implementation or acceptance claim is
made by activation alone.

**Outcome**: the orchestrator can bind different goal types without exposing internal modules.

Initial routing families:

- finite phone-operation goal -> canonical Kernel task;
- long-lived/waiting-driven application goal -> ApplicationRuntime/scheduler capability;
- active GoalRun follow-up/control -> existing GoalRun;
- language-only result -> local language capability;
- experience collection -> internal part of execution, not a user LearningJob choice.

Soul is the first long-lived external-owner compatibility candidate. The user enters through the same goal composer; `soul-reply-v1` and dating-copilot ownership remain internal bindings.

The confirmed long-lived policy is continuous: candidate milestones notify without auto-pausing or completing the goal. Each event-driven cycle is bounded and the GoalRun stays active until explicit user stop/takeover/revision, or enters a resumable wait for a real gate.

**Proof**: at least one finite phone goal and one long-lived external-event goal run through the same ingress, controls, projection, and evidence discipline; the long-lived goal survives candidate notification and continues until an explicit stop test.

### U9 — Production autonomy, deployment, and broader applications

**Outcome**: sustained operation on explicit production targets with install policy, observability, backups, upgrade/rollback, resource controls, privacy, and configurable higher-risk action policy.

Includes only after earlier exits:

- one plan-level approval followed by automatic download/install/configure/start/verify for missing components;
- multi-device scheduling if required;
- production application policies;
- durable audit and retention;
- long-run and failure-injection gates;
- versioned release artifacts;
- installation/upgrade/rollback proof;
- additional applications and cross-app goals;
- optional offline training pipeline, separately governed.

## 4. Product-track status table

| Track | Status at specification creation | Product claim allowed |
|---|---|---|
| U0 Canonical specification | DONE | New direction documented; no product behavior changed |
| U1 Goal facade | DONE | Durable v2 GoalRun facade over MobileTask; U3 now supplies the independent completion gate |
| U2 One UI + preflight | PARTIAL | Managed preflight/repair and a real settings/battery compatibility run exist; primary-composer visual/reopen proof remains unavailable |
| U3 Qwen + final verifier | DONE | Frozen criteria, independent evidence-referenced completion, gated learning, real settings/battery completion, and measured Qwen visual default |
| U4 Experience ledger | DONE | Additive evidence ledger, reversible policies, controlled thresholds, and real attributable 10-to-5-action settings warm run |
| U5 STZB learning vertical | PARTIAL, non-blocking | Checklist and experience gates exist; real complete daily cold/warm acceptance remains a stabilization backlog |
| U6 Kernel worker canary | DONE | Explicit Kernel canary completed settings/battery/Home, controls, final verification, and Experience reuse; default cutover remains out of scope |
| U7 Real cutover | DONE | Local normal launcher, real Kernel GoalRun, controls, restore/rollback, final active restart, and 30-minute local observation passed |
| U8 Long-lived routing | IN PROGRESS | Work order activated; implementation and acceptance not yet claimed |
| U9 Production hardening | Not started | None |

## 5. Advancement rule

An AI may complete multiple implementation steps inside one work order without pausing. It may advance to the next work order only when:

- the user authorized roadmap execution rather than only documentation;
- the current work order's automated and specified runtime checks pass;
- current-state and work-order status are updated;
- no new user choice materially changes product behavior;
- rollback remains available;
- the next work order does not require an unresolved decision in `09_DECISIONS_AND_OPEN_QUESTIONS.md`.

An explicit owner instruction may advance the roadmap past a `PARTIAL`
milestone when the incomplete evidence is isolated, recorded without being
relabelled, the next slice is additive and reversible, and no non-negotiable
correctness gate is waived. The incomplete milestone then becomes a named
non-blocking stabilization track. This changes scheduling, not evidence truth:
it cannot be used to claim the partial milestone `DONE`, production-ready, or
accepted on evidence that was never obtained.

Hard work is not a reason to stop. A real missing authority, external gate, destructive migration risk, or product choice is.
