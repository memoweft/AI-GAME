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

## 2. Current open decisions

There is no known product decision currently blocking U1-U9. Implementation may still discover a new material choice; it records and asks that question under `08_AI_EXECUTION_PROTOCOL.md` instead of guessing.

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
