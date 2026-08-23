# Target architecture

## 1. Architectural outcome

The user sees one durable GoalRun. The platform may internally compose several capabilities, but it preserves one user goal, one truthful result projection, and one auditable chain of physical ownership.

```text
                         User
                          |
                   Universal Goal API
                          |
                       GoalRun
                          |
             Goal Orchestrator (Qwen default)
             +------------+-------------+
             |                          |
      Capability Registry       Environment Manager
             |                          |
             +------------+-------------+
                          |
                 Execution Binding Plan
                          |
                   Goal Coordinator
                          |
                     Runtime Kernel
        +-----------------+------------------+
        |                 |                  |
 Model Role Router   Ownership Broker   Experience Service
        |                 |                  |
 role bindings       ADB / owner APIs    episode / policy
        |                 |                  |
        +-----------------+------------------+
                          |
                 Evidence and Projection
                          |
                        User UI
```

## 2. Canonical objects

### 2.1 GoalRun

The one user-visible durable object.

Minimum fields:

```text
id
original_goal
normalized_intent
user_constraints
success_criteria
execution_status
control_state
resume_execution_status
active_stage
binding_plan_revision
environment_state
experience_scope
completion_verification
result_summary
verified_facts
uncompleted_items
waiting_reason
created_at / updated_at / terminal_at
```

`GoalRun` does not flatten every internal module into one schema. It stores stable product truth and references module-specific execution state when a capability adapter owns deeper semantics.

### 2.2 GoalSpecification

A versioned structured interpretation of the user request:

```text
goal family
target application or discoverable application class
one-shot / repeatable / long-lived
continuation policy, including continuous-until-stop when applicable
notification milestones that do not imply completion
observable success criteria
external outcomes
allowed environment scope
preferred autonomy mode
known constraints
unknowns that can be discovered
unavoidable external gates
```

The original user text remains authoritative. Replanning cannot silently narrow the success criteria.

### 2.3 CapabilityBindingPlan

Records which internal capabilities will execute the current revision and why:

```text
orchestrator binding
visual operator binding
verification binding
device or owner binding
short task / long-lived cycle / scheduler binding
experience scope
fallbacks
configuration actions
```

No physical action may occur while a plan is merely being classified. The plan is persisted before execution.

### 2.4 Stage

A currently pursued observable sub-outcome, not an arbitrary model thought and not a precomputed coordinate sequence.

Each Stage has:

- objective;
- evidence-based completion condition;
- relevant constraints;
- current attempt/recovery budget;
- relationship to original success criteria.

### 2.5 GoalCompletionVerification

An independent final decision based on frozen success criteria and verified facts. It answers whether the user's original purpose was achieved, partially achieved, failed, or remains externally unresolved.

It must not simply ask the Planner whether its own plan is done.

## 3. Universal Goal API

The target primary interface is conceptually:

```text
POST   /api/v2/goals
GET    /api/v2/goals
GET    /api/v2/goals/{goal_id}
POST   /api/v2/goals/{goal_id}/messages
POST   /api/v2/goals/{goal_id}/controls
GET    /api/v2/goals/{goal_id}/events
GET    /api/v2/goals/{goal_id}/events/stream
```

Create input contains the goal and an idempotency key. Device, model, runtime, profile, skill, and owner ids are not required in the ordinary user request. Advanced deployment policy may constrain their selection outside the primary goal body.

The first work order defines the exact contract and may initially bind all supported device goals to the existing MobileTask path. It must expose that compatibility binding honestly.

## 4. Goal Orchestrator

The orchestrator is the high-level decision engine. Target responsibilities:

- normalize the goal without losing original intent;
- identify observable success criteria;
- decide whether the work is one-shot, repeatable, long-lived, or waiting-driven;
- request environment discovery before asking the user;
- select capabilities through the Registry;
- create or revise Stages;
- decide when to recover, replan, wait, or stop;
- retrieve relevant experience;
- request final goal-completion verification;
- explain progress and result.

It does not directly send ADB input. It does not bypass the Kernel or ownership broker.

The confirmed default language/planning binding is the local Qwen3.8 27B deployment and the initial visual binding is GUI-Owl. If live discovery proves that Qwen also supports vision, U3 compares both visual bindings on the same fixtures and real-device evidence, then selects the better measured default. The model choice never leaks into the generic GoalRun, Kernel, ownership, verification, or experience contracts.

## 5. Capability Registry and adapters

The Registry describes what is actually available, not what code theoretically exists.

Example capabilities:

```text
android.observe
android.tap
android.long_press
android.swipe
android.type
android.back
android.home
android.launch_app
local.text_reasoning
local.visual_grounding
local.goal_verification
long_lived.wait
local.managed_notification
external_owner.soul
experience.retrieve
experience.record
```

Each descriptor includes:

- provider and version;
- health and readiness;
- required configuration;
- target scope;
- ownership semantics;
- input/output contract;
- known limits;
- whether AI-GAME owns its lifecycle;
- evidence it produces.

Profiles may package descriptors, hints, and verifiers. They are optional internal adapters, not user-facing modes and not generic keyword firewalls.

## 6. Environment Manager

The manager produces an evidence-backed environment state and a reversible configuration plan.

Responsibilities:

- discover models, endpoints, devices, emulators, ADB executables, packages, owner services, storage, and current leases;
- determine what is ready, fixable, missing, or externally blocked;
- start and reconnect dependencies whose lifecycle AI-GAME owns;
- apply validated non-secret local configuration atomically;
- create a concrete missing-install/security-change plan with scope, sources/integrity, mutations, rollback, and expected readiness evidence;
- persist one user confirmation for that exact plan, then automatically download, install, configure, start, and verify without step-by-step prompts;
- resume the same GoalRun after repair;
- never report readiness from configuration text alone.

Approval is plan-scoped. Materially broadening components, security boundaries, third-party terms, account creation, or destructive replacement creates a new plan and a new single confirmation before writes.

An unavoidable external action becomes a typed gate, for example:

```text
USB_DEBUG_AUTHORIZATION
ACCOUNT_LOGIN
CAPTCHA_OR_BIOMETRIC
MISSING_CREDENTIAL
SYSTEM_PERMISSION_CONFIRMATION
REMOTE_SERVICE_UNAVAILABLE
TIME_OR_INBOUND_EVENT
```

The user sees one plain instruction, not implementation diagnostics.

## 7. Goal Coordinator and RuntimeKernel

The coordinator drives the canonical loop:

```text
plan or select Stage
-> acquire/confirm ownership
-> observe
-> retrieve scene-conditioned experience
-> request one action
-> validate action against current revision and observation
-> execute
-> obtain new observation
-> verify immediate effect
-> record transition
-> commit fact, recover, or replan
-> verify Stage
-> verify original Goal when candidate-complete
```

The target Kernel owns the facts and fences. Model adapters propose decisions; they do not mutate task truth.

The coordinator must add what the current Kernel lacks:

- model role ports;
- serial worker lifecycle;
- ADB executor production binding;
- task-session ownership rather than only momentary action ownership;
- planner/replanner and final verifier;
- pause/resume/cancel/takeover fences;
- restart-first observation and uncertain reconciliation;
- final result projection.

## 8. Ownership Broker

All physical mutation must be traceable to one active owner binding.

The broker coordinates:

- Kernel ADB leases;
- compatibility MobileTask leases during migration;
- GameLearning device work during migration;
- Chat device work during migration;
- explicitly bound specialized external owners.

It may delegate physical action to an external owner, but the GoalRun stores
that binding and reconciles the owner's authoritative ledger. For an
external-owner plan, it first persists an opaque, plan-scoped
`owner_binding_ref`; that reference associates later owner evidence with the
frozen GoalRun, but is neither an endpoint, credential, nor account identity.

ApplicationRuntime owns scheduling, event waits, continuation, and per-cycle
budgets for a long-lived application GoalRun. When its target is an Android
application, each bounded physical cycle goes through the Goal Coordinator and
RuntimeKernel under the same Ownership Broker:

```text
ApplicationRuntime wait/event lifecycle
-> RuntimeKernel observe/action/re-observe/verify cycle
-> ApplicationRuntime commit/wait/continue
```

An application already logged in on the authorized device does not require a
specialized external owner. The Registry may select such an owner only as an
optional available capability; once selected, that adapter must satisfy its own
binding, authorization, receipt, and reconciliation contract. The broker never
starts a competing ADB worker for the same target.

[constraint-source: USER_DECISION; ref: D22 U8 real mobile/application correction 2026-08-23]

## 9. Experience Service

The service presents one canonical experience contract while old databases are migrated incrementally through adapters. A forced all-at-once database merge is not required.

It stores:

- episode and goal provenance;
- semantic scenes;
- immediate transitions;
- delayed outcomes;
- success, failure, and recovery candidates;
- immutable policy revisions;
- compatibility and confidence;
- rollback and deprecation state;
- retrieval and actual-use attribution.

Detailed semantics are in `04_AUTONOMY_AND_LEARNING.md`.

## 10. Migration role of current modules

| Current module | Target role |
|---|---|
| MobileTaskRuntime | First compatibility execution binding and source of proven device-loop behavior; later retired behind Kernel coordinator |
| RuntimeKernel | Canonical task/evidence/control/ownership runtime after its autonomous worker is complete |
| TaskGateway | Compatibility source for parts of the v2 goal API and projections |
| GameLearning | Source adapter for transition provenance and policy concepts; separate primary UI retired |
| ApplicationRuntime | Capability adapter for waiting-driven and long-lived application cycles |
| Soul composition | Specialized external-owner capability selected by a universal goal, not a top-level product |
| Legacy Chat | Compatibility capability; ordinary device goals enter through GoalRun |
| SkillMemory | Legacy success hint imported only after validation |
| PolicyMemory | Legacy positive-transition hint imported with provenance |

## 11. Dependency direction

```text
Goal API
-> Goal service and orchestrator ports
-> Coordinator / Kernel ports
-> capability, model, device-owner, environment, experience ports
-> concrete adapters
```

The generic core may not import an application package, a model provider SDK, an ADB command builder, or a specialized owner implementation.

## 12. Compatibility and rollback

- Add the v2 goal facade before removing any v1 route.
- Bind the first v2 goals to the existing MobileTask path while labeling the binding.
- Migrate one verified vertical to Kernel canary before runtime cutover.
- Never run Legacy and Kernel workers against the same device GoalRun.
- Keep old databases readable until their replacement data and rollback are verified.
- Use explicit compatibility namespaces; do not guess contract version from payload shape.
- A failed milestone rolls back its new binding, not user history or evidence.
