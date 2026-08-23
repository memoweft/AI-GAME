# Model responsibilities and automatic configuration

## 1. Confirmed baseline and measured visual binding

The product direction discussed with the user is:

```text
local Qwen3.8 27B
  -> understand goal
  -> produce/revise GoalSpecification and Stage plan
  -> select capabilities
  -> reason over bounded experience
  -> reflect and explain

local GUI-Owl
  -> inspect the current phone screenshot
  -> ground visible UI targets
  -> propose one supported atomic phone action
  -> provide bounded visual facts for verification

Runtime / verifiers
  -> own facts, fences, transport truth, result state, and evidence gates
```

This was the confirmed initial split. On 2026-08-21 live discovery established an
installed Qwen3.8 27B multimodal OpenAI-compatible binding, and the current source
gained a provider-neutral forced-tool adapter for planner, visual action,
verification, and reflection roles. At that pre-U3 snapshot, the development
candidate used one Qwen process with separated role contracts and still awaited
the required serial comparison with GUI-Owl.

U3 completed that comparison on 2026-08-21 using the same five persisted real
MuMu frames. Qwen matched the successful action path on 5/5 frames in 34.311
seconds; GUI-Owl matched 3/5 in 6.710 seconds and chose application Back instead
of Stage finish and system Home on the last two frames. Qwen is therefore the
accepted current visual default. GUI-Owl remains an explicit serial diagnostic/
rollback binding, not a silent runtime fallback.

This selection changed the configured role binding, not the GoalRun, Kernel,
evidence, or experience contracts.

## 2. Role contracts

The core depends on capabilities, not model names.

### Goal orchestrator

Inputs:

- original goal and revisioned constraints;
- current GoalSpecification;
- verified facts and current Stage;
- environment/capability summary;
- bounded recent outcomes;
- scene-conditioned experience packet.

Outputs:

- structured GoalSpecification or revision;
- binding requirements;
- one observable Stage and criteria;
- recover/replan/wait/finish decision;
- final user-language content derived from verified results.

It does not output executable ADB commands.

### Visual operator

Inputs:

- latest screenshot and available device facts;
- current Stage and expected outcome;
- supported atomic action schema;
- bounded relevant positive, negative, and recovery experience.

Outputs exactly one of:

- grounded atomic action plus expected visual effect;
- wait;
- re-observe;
- no-action with reason;
- candidate terminal visual fact.

### Immediate verifier

Uses fresh after evidence, the expected outcome, and bounded before facts. It returns success, failure, or uncertainty with evidence references. Implementation may be deterministic, visual-model assisted, Qwen-assisted over structured visual facts, or hybrid.

### Goal Completion Verifier

Uses the original goal, frozen success criteria, verified facts, and admissible external outcomes. It is independent from plan generation. No single free-form model sentence directly changes terminal state.

## 3. Model capability registry

Every binding publishes:

```text
binding id and version
roles/capabilities
modalities
structured output format
context and media limits
endpoint and health
local/cloud data boundary
latency class
lifecycle owner
fallback compatibility
```

Core runtime code must not branch on `qwen`, `gui-owl`, a provider brand, or a model filename. Adapters may implement model-specific parsing.

## 4. Qwen integration prerequisites

Before the Qwen work order can claim success, discover and record:

- exact local model identifier and quantization;
- endpoint protocol and served name;
- context size and practical token budget;
- supported structured output/tool calling;
- whether it accepts images;
- startup/shutdown ownership;
- average planning latency;
- Chinese goal and JSON-schema conformance;
- how it receives bounded experience;
- fallback behavior when unavailable.
- if image input is supported, comparative visual-grounding/action quality, latency, recovery, and resource results against GUI-Owl.

Live discovery on 2026-08-21 found the explicitly configured loopback endpoint,
model alias, multimodal projector, and owned lifecycle script. The machine-local
binding is selected through the ignored `config/mobile-role-runtime.env`; the
tracked example contains no credential. This discovery proves availability for the
inspected machine, not a portable installation or deployment artifact.

U4 retained the U3 role split and tuned the schema-only GoalSpecification call
to the same non-thinking forced-tool baseline as final completion. This removed
repeated full-budget hidden-reasoning stalls in the inspected local binding;
ordinary visual decisions and immediate verification remain separate calls.
The real U4 evidence also preserves a failed model-outage episode rather than
treating endpoint recovery as task success.

## 5. Automatic environment loop

Preflight state machine:

```text
DISCOVER
-> ASSESS
-> PLAN_REPAIR
-> APPLY_REVERSIBLE_REPAIR
-> VERIFY_READY
-> READY | WAITING_EXTERNAL | FAILED
```

### Discover

Inspect without mutation:

- AI-GAME services and ports;
- configured and discovered local model endpoints;
- model capability metadata;
- ADB executables and server state;
- connected Android targets;
- installed/available emulator managers;
- target package state;
- existing leases and owners;
- storage and database readiness;
- specialized owner service capabilities;
- saved non-secret settings.

### Assess

Classify every prerequisite:

```text
READY
FIXABLE_AUTOMATICALLY
MISSING_INSTALLABLE
WAITING_EXTERNAL
INCOMPATIBLE
UNKNOWN
```

### Apply reversible repair

Examples:

- reconnect a known loopback ADB target;
- synchronize the current MuMu port;
- start a model service whose lifecycle AI-GAME owns;
- select the sole compatible idle device;
- refresh a stale non-secret endpoint;
- create missing runtime directories or additive schema;
- restart a failed owned component after identity checks.

Every repair is recorded and verified. Configuration text is not readiness evidence.

### External gate

Examples:

- approve USB debugging on the phone;
- provide a missing credential;
- complete CAPTCHA, biometric, or system permission interaction;
- start a third-party component whose lifecycle is not delegated;
- resolve an account lock or unavailable remote service.

The UI shows one plain instruction and a retry/continue state. It does not lose the GoalRun.

## 6. Install and machine-mutation policy

Automatic startup and reversible configuration of already installed, AI-GAME-owned components are in the target product.

Downloading models, installing software, or changing operating-system security settings requires one concrete plan-level confirmation before the first mutation. After that approval, the Environment Manager performs the approved download/install/configure/start/verify sequence automatically and resumes the same GoalRun without asking at every step.

An approval is scoped to the presented plan. A materially broader component, different security boundary, new third-party terms, account creation, or destructive replacement requires a new one-time confirmation. This confirmed policy does not make ordinary startup and reversible repair manual.

## 7. Target selection

Selection order:

1. target already bound to the active GoalRun;
2. explicitly constrained target from user/deployment policy;
3. sole compatible, ready, idle target;
4. best compatible target by capability and ownership;
5. plain-language user choice only when multiple materially different valid targets remain.

The system never silently switches an active GoalRun to a different physical device after binding.

## 8. Data boundary

The default target is local execution. Raw screenshots stay local unless an explicit deployment binding permits otherwise. Every model call records which binding received which form of data. The product core remains compatible with optional cloud language or reasoning bindings, but cloud availability is not required by the target architecture.
