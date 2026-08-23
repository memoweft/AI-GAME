# Product specification: universal local mobile AI operator

## 1. Product purpose

The user should be able to say:

```text
帮我把率土之滨今天的每日任务做完。
```

or:

```text
帮我在手机上的社交软件里认识适合长期相处的人。
```

or:

```text
打开设置，看看电池情况，告诉我以后回到桌面。
```

AI-GAME then owns the operational work:

```text
understand goal
-> discover capabilities and environment
-> configure or repair dependencies
-> choose target device and application
-> plan observable stages
-> observe phone
-> take one grounded action
-> observe again
-> verify effect
-> recover or continue
-> retain reusable experience
-> report an evidence-backed result
```

The user should not need to understand ADB, model endpoints, profiles, skill ids, runtime modes, databases, task state machines, or application-specific workspaces.

## 2. Primary experience

The main product surface has one composer:

```text
What do you want me to accomplish?
```

After submission the default view shows only useful progress:

- what goal is active;
- which device or external capability is being used;
- what stage is being pursued;
- whether the system is acting, verifying, recovering, waiting, or finished;
- what the user can pause, resume, stop, change, or take over;
- the final verified result and any uncompleted part.

Screenshots, model bindings, ADB receipts, events, experience revisions, and diagnostics remain available in an expandable evidence view. They are not separate products.

## 3. User contract

The user is responsible for:

- stating the desired outcome and any explicit preference;
- making a physical or account confirmation only when no software path can do it;
- stopping or taking over when desired;
- confirming external outcomes that cannot be established from device evidence alone.

AI-GAME is responsible for:

- interpreting the goal and asking no unnecessary setup questions;
- inspecting before asking;
- automatically choosing and configuring available local capabilities;
- keeping one canonical GoalRun and honest progress state;
- making and revising a plan;
- operating the device through a single owner;
- verifying every claimed effect against fresh evidence;
- recovering from ordinary mistakes without requiring step-by-step correction;
- preserving useful success, failure, and recovery experience;
- adapting old experience to the current screen instead of replaying blind coordinates;
- reporting exactly what was completed, what remains, and why.

## 4. Autonomy levels

The first implementation uses `development_open` autonomy on an explicitly authorized local device or emulator.

`development_open` means:

- no generic core blacklist based only on words such as claim, recruit, strengthen, deploy, chat, match, purchase, or pay;
- the orchestrator may plan any action supported by the configured device owner;
- application behavior is learned and verified instead of being restricted to a tutorial menu;
- application-specific policies are optional configuration, not hard-coded product identity.

It does **not** mean:

- multiple workers may control one device;
- a stopped task may continue;
- malformed model output may execute;
- an old observation may authorize a new action;
- uncertain physical input may be replayed;
- the runtime may click forever;
- transport success may be reported as task success;
- credentials, funds, or permissions exist when they do not;
- an external human outcome can be fabricated.

Additional production safety policies are a later milestone and must be layered over, not substituted for, the autonomous execution loop.

## 5. Goal semantics

A `GoalRun` is the one user-visible durable execution object. It may internally use a short device task, a long-lived application cycle, a scheduler, a specialized owner, or several capability adapters. Those internal choices do not create separate user tasks unless the goal actually has independent child outcomes.

Canonical lifecycle:

```text
ACCEPTED
-> PREFLIGHT
-> PLANNING
-> RUNNING
<-> VERIFYING
<-> RECOVERING
<-> WAITING_CONFIGURATION | WAITING_EXTERNAL
-> CANDIDATE_COMPLETE
-> COMPLETED | PARTIAL | FAILED | CANCELLED | UNCERTAIN
```

Meanings:

- `COMPLETED`: all runtime-defined observable success conditions are verified.
- `CANDIDATE_COMPLETE`: the bound executor has no more planned work, but the original goal has not yet passed independent completion verification; this is non-terminal and must not promote successful experience.
- `PARTIAL`: a useful subset is verified and the remaining subset is explicitly known.
- `WAITING_CONFIGURATION`: a required runtime capability is not ready and the current milestone cannot repair or select it automatically; the same GoalRun can resume after configuration becomes ready.
- `WAITING_EXTERNAL`: progress requires a human, remote service, elapsed time, incoming message, credential, physical confirmation, or other unavailable fact.
- `FAILED`: a definite failure is established and recovery policy is exhausted.
- `UNCERTAIN`: a physical or external effect cannot be resolved safely.

For an open-ended social goal, finding a candidate or maintaining a conversation does not prove a relationship. The system can verify controllable stages and request a user-confirmed external outcome; it cannot promise another person's choice. Under the confirmed continuous policy, a candidate milestone creates an evidence-backed notification but does not pause or complete the GoalRun. The long-lived goal continues until the user explicitly stops, takes over, or revises it, while real external/configuration gates produce a resumable wait.

GoalRun freezes two separate state axes:

```text
execution_status:
ACCEPTED | PREFLIGHT | PLANNING | RUNNING | VERIFYING | RECOVERING |
WAITING_CONFIGURATION | WAITING_EXTERNAL | CANDIDATE_COMPLETE |
COMPLETED | PARTIAL | FAILED | CANCELLED | UNCERTAIN

control_state:
AUTOMATED | PAUSE_REQUESTED | PAUSED | TAKEOVER | STOP_REQUESTED
```

`PAUSED` and `TAKEOVER` fence all new automated physical actions but do not manufacture a business outcome or silently change verified facts. `WAITING_CONFIGURATION` and `WAITING_EXTERNAL` are execution conditions, not user pause. Resume returns control to `AUTOMATED` and must observe again before acting. A settled stop makes `execution_status=CANCELLED`; controls unsupported by a compatibility binding return an explicit capability error instead of a fake success.

## 6. Universal, not application-specific

Applications provide observations and affordances. They do not define top-level product modes.

Internal adapters may exist for package discovery, specialized owners, long-running schedulers, or stronger verifiers. Their rules must be capability-scoped and replaceable. The generic orchestrator remains responsible for the user's goal and final truth.

The following are implementation capabilities, not primary navigation products:

- MobileTaskRuntime;
- RuntimeKernel;
- Gateway Task service;
- ApplicationRuntime;
- Soul `soul-reply-v1`;
- GameLearning;
- Chat;
- LearningProfile or ApplicationProfile;
- legacy Workflow, Run, and Approval resources.

## 7. Learning promise

For a repeatable goal family, later executions should measurably improve:

- fewer wrong-page transitions;
- fewer repeated no-effect actions;
- faster recognition of known scenes;
- more successful automatic recovery;
- higher verified completion rate;
- lower action count and elapsed time when the environment is comparable;
- correct invalidation when an application update makes old experience stale.

The platform must not claim learning merely because it stored a transcript or reused a prompt. Improvement requires evidence-linked experience and repeat-run metrics.

## 8. Configuration promise

The default path is inspect, repair, and continue:

1. discover local models, Android targets, application packages, owner services, and saved non-secret configuration;
2. select a compatible binding;
3. apply safe reversible local configuration automatically;
4. start or reconnect a managed local dependency when that lifecycle is owned by AI-GAME;
5. if a required component is missing or a system-security change is needed, present one concrete plan for confirmation;
6. after that one approval, automatically download, install, configure, start, verify, and continue within the approved plan;
7. expose one concise `WAITING_EXTERNAL` instruction only when another external gate cannot be automated;
8. resume the same GoalRun after the gate is satisfied.

The primary flow must not send the user to manually edit environment files or choose internal modules.

## 9. First proof scenarios

The platform is proved through three different verticals, not through three separate products:

1. **General phone operation**: a settings/battery goal demonstrates goal-to-device generality without application-specific scripting.
2. **Repeat-run learning**: a user-authorized STZB daily goal demonstrates exploration, verification, recovery, memory promotion, and measurable improvement over repeated runs.
3. **Long-lived mobile/application outcome**: a real application or social goal
   demonstrates automatic discovery of the authorized device and logged-in app,
   scheduling and incoming-event waits, bounded RuntimeKernel phone cycles,
   fresh post-action verification, conversation/task continuity, delayed
   outcome learning, candidate notification without automatic termination,
   restart continuity, and continuous operation until explicit stop. A
   specialized external owner is optional; its separate account, receipt, and
   reconciliation requirements apply only when the frozen binding plan actually
   selects that adapter.

[constraint-source: USER_DECISION; ref: D22 U8 real mobile/application correction 2026-08-23]

Passing one vertical does not imply passing the others.

## 10. Product success

AI-GAME succeeds when a non-technical user can state an outcome once and the system can carry the task substantially farther than a scripted demo while remaining honest about real-world state.

The decisive question for every feature is:

> After the user states the purpose, does this make the platform more capable of autonomously completing the remaining work and doing it better next time?
