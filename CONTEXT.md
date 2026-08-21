# AI-GAME canonical domain context

> Product authority: `docs/product/00_INDEX.md`
>
> This context replaces the previous module-first domain glossary.

AI-GAME is a local-first universal mobile AI operator. A user states an outcome; the platform discovers and configures available capabilities, plans, operates the phone, verifies effects, recovers, learns from evidence, and reports an honest result. Games, social applications, settings, shopping, and other apps are target environments, not separate products.

## Product-level terms

### GoalRun

The one user-visible durable execution object. It owns the original goal, revisioned constraints, success criteria, current binding, execution status, orthogonal control state, progress, verified facts, waiting reason, result, and evidence projection.

Its `execution_status` includes non-terminal `WAITING_CONFIGURATION`, `WAITING_EXTERNAL`, and `CANDIDATE_COMPLETE` states in addition to active phases and honest terminal outcomes. `CANDIDATE_COMPLETE` means the bound executor ended its generated plan but independent original-goal verification is still pending; it cannot promote successful experience.

Its separate `control_state` is `AUTOMATED`, `PAUSE_REQUESTED`, `PAUSED`, `TAKEOVER`, or `STOP_REQUESTED`. Pause/takeover fence automation without pretending the business goal succeeded or failed; a settled stop produces terminal `CANCELLED` execution status.

Avoid using `MobileTask`, `ApplicationInstance`, `LearningJob`, `Run`, `Workflow`, or a chat turn as a synonym. Those are current or internal capability objects.

### GoalSpecification

A versioned structured interpretation of the original goal: goal family, application clues, finite/repeatable/long-lived nature, continuation policy, non-terminal notification milestones, observable success criteria, external outcomes, environment requirements, and unavoidable gates. The original user text remains authoritative. The confirmed long-lived default is continuous-until-explicit-stop; candidate notification alone never pauses or completes it.

### CapabilityBindingPlan

The persisted decision that selects orchestrator, visual operator, verifier, device or specialized owner, short/long-lived execution capability, experience scope, fallbacks, and configuration actions. Classification alone may not trigger a physical action.

### Stage

One current observable sub-outcome with an evidence-based completion condition. A Stage is not a click, coordinate list, model thought, or arbitrary workflow node.

### Observation

A time- and target-bound factual snapshot such as screenshot, package/activity, UI tree, orientation, device state, and obstruction/loading state. A stale Observation cannot authorize a new physical action.

### ActionTransition

The evidence chain joining a before Scene, one semantic/grounded action, transport fact, after Scene, immediate outcome, and optional recovery. Transport acceptance is never outcome success.

### GoalCompletionVerification

An independent comparison of the original goal's frozen success criteria with verified facts and admissible external outcomes. Completion of all Planner-generated Stages is insufficient by itself.

### EnvironmentGate

A durable `WAITING_EXTERNAL` condition that software cannot currently resolve, such as first-time USB authorization, missing credential, CAPTCHA/biometric confirmation, remote outage, elapsed time, or incoming event. Satisfying the gate resumes the same GoalRun.

### ConfigurationGate

A durable `WAITING_CONFIGURATION` condition for a required runtime capability that is not ready and cannot yet be repaired or selected automatically. It preserves the same GoalRun and differs from a user pause or an external-world wait.

## Autonomy and learning terms

### ExperienceEpisode

One bounded execution attempt under a GoalSpecification revision, environment/model/policy binding, and frozen criteria. It can end in success, failure, partial, waiting, cancellation, or uncertainty.

### SceneState

A semantically retrievable representation of one Observation, including application identity, scene, visible anchors, normalized regions, obstruction, device/UI compatibility, and evidence reference. Screenshot hash alone is not a reusable scene.

### OutcomeSignal

An evidence-linked immediate or delayed signal: success, failure, no progress, wrong scene, recovered, task result, later response/no-response, or user approval/rejection. It is independent of transport status.

### ExperienceCandidate

A scoped, evidence-backed proposed positive action rule, known failure rule, or recovery rule. A candidate is not active policy and may be validated, promoted, rejected, deprecated, or superseded.

### PolicyRevision

An immutable set of validated, scene-conditioned experience rules with compatibility, confidence, provenance, active head, and rollback. It is not a model checkpoint or coordinate macro.

### Learning

Measurable repeat-run improvement attributable to retrieved experience: better verified completion, fewer wrong/no-effect actions, faster recovery, lower action/time cost, and no increase in false completion. A memory row, prompt reuse, or model claim alone is not learning.

## Runtime invariants

- One physical target has one active mutation owner.
- The user can stop or take over.
- Revision, control, target, ownership, and current Observation are checked before action.
- Fresh after evidence is required for effect claims.
- Verify precedes Commit.
- Uncertain physical effects are reconciled, not replayed.
- Recovery is bounded; no-progress cannot click forever.
- Original Goal coverage is verified independently before completion or successful experience promotion.
- Current evidence outranks old experience.
- Experience is isolated by user/account/person/application/goal and compatibility scope.

These are correctness constraints even in `development_open` autonomy. Application-specific keyword bans are optional policy and are not generic Runtime invariants.

## Current implementation terms during migration

### MobileTaskRuntime

The current compatibility engine with the real general Android natural-language loop. It remains active until RuntimeKernel proves an equivalent business GoalRun and rollback.

### RuntimeKernel

The target canonical task/evidence/control runtime. Current code has strong primitives but lacks a default production autonomous worker, complete model-role binding, and normal-launcher business-goal evidence.

### ApplicationRuntime

An internal capability for long-lived observation/policy/owner/verification cycles. Soul is its current production profile; it is not a primary product entry.

### GameLearning

A legacy bounded learning capability and source of transition/policy provenance concepts. Experience learning becomes part of ordinary GoalRun execution; the user should not need to choose a LearningJob.

### SkillMemory / PolicyMemory / Soul reply learning

Existing separate memory mechanisms. They are legacy sources or adapters, not the canonical vNext Experience Service. Their existence does not prove repeat-run improvement.

### Profile

An internal bundle of capability metadata, application knowledge, verifier, owner binding, or optional policy. It is not a product mode and does not have authority to redefine the universal user's goal.

## Model roles

Current target assumption:

- local Qwen3.8 27B: goal orchestration, criteria, planning, routing, reflection, memory reasoning, and language;
- local GUI-Owl: visual grounding and one atomic phone action;
- Runtime/hybrid verifiers: factual immediate and final decisions.

Core contracts depend on capabilities, not model names. The confirmed baseline is Qwen3.8 27B for orchestration and GUI-Owl for visual phone actions; a visual-capable Qwen may replace the visual binding only after comparative evidence. See `docs/product/05_MODELS_AND_AUTO_CONFIGURATION.md` and decision D11.
