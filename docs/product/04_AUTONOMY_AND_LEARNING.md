# Autonomy, recovery, and experience learning

## 1. Required behavior

The platform must improve through repeated evidence-backed execution without requiring a human to label every wrong click.

The learning loop is:

```text
GoalRun
-> frozen success criteria
-> Episode
-> Scene
-> proposed Action
-> transport fact
-> next Scene
-> immediate Outcome
-> optional Recovery
-> Stage/Goal outcome
-> optional delayed Outcome
-> Experience Candidate
-> validation
-> immutable PolicyRevision
-> scene-conditioned retrieval in a later run
```

## 2. What “the system knows it clicked wrong” means

The system does not know from ADB exit code. It knows only when admissible evidence contradicts the expected outcome or establishes another result.

Examples:

- expected task list, observed character detail -> `WRONG_SCENE`;
- expected visible transition, identical relevant scene -> `NO_PROGRESS`;
- expected claimed reward, button remains claimable -> `IMMEDIATE_FAILURE` or `UNCERTAIN` depending on evidence;
- wrong scene followed by Back and a recognized prior scene -> `RECOVERED`;
- missing/stale/obscured observation -> `UNCERTAIN`, not failure memory;
- all generated Stages complete but original daily checklist remains incomplete -> Goal completion failure, not success.

Only evidence-confirmed failure can become negative experience. Uncertainty must not create a permanent “never click” rule.

## 3. Canonical learning data

### 3.1 ExperienceEpisode

```text
episode_id
goal_run_id
goal_spec_revision
goal_family
application identity
user/account isolation scope
device class and orientation
application/UI version hints
model/prompt/policy versions
frozen success criteria
initial scene
terminal outcome
action / recovery / intervention counts
timestamps
```

### 3.2 SceneState

```text
scene_id
observation_ref and integrity hash
package/activity when available
semantic scene label
visible anchors and normalized regions
dialog/obstruction/loading state
orientation and resolution
capture age
confidence
```

The raw screenshot hash alone is not a retrieval key. Small rendering changes must not erase semantic similarity, and a visually similar but semantically different account state must not be treated as identical.

### 3.3 ActionTransition

```text
transition_id
episode_id
stage/objective
before_scene_id
semantic action and grounded target
resolved coordinates for audit only
expected outcome
transport status
after_scene_id
immediate outcome
progress and stage verdict
failure class
recovery link
latency
```

Coordinates are execution evidence, not the learned procedure.

### 3.4 OutcomeSignal

Supported signals include:

```text
immediate_success
immediate_failure
no_progress
wrong_scene
recovered
task_success
task_failure
delayed_positive
delayed_negative
no_response
user_approval
user_rejection
```

Every signal has a source, evidence reference, observation time, attribution target, confidence, and reward vector. A single scalar “reward” is insufficient for action correctness, progress, cost, recovery, user satisfaction, and delayed external results.

### 3.5 ExperienceCandidate

Candidates are scene-conditioned statements such as:

```text
When goal family is STZB daily,
Stage objective is open the task list,
and the current scene is a recognized main city with the task anchor visible,
click the task anchor;
expect the task-list scene.
```

or:

```text
In this main-city variant, clicking the nearby character anchor entered a character detail.
Do not prioritize that grounded target for the task-list objective.
If already in character detail, Android Back returned to the recognized main city.
```

Candidate fields include scope, scene matcher, objective matcher, recommendation or known failure, expected next scene, recovery, support/failure counts, confidence, provenance transitions, compatibility, and status.

### 3.6 PolicyRevision

An immutable version containing validated positive, negative, and recovery rules. It supports active head, rollback, superseded/deprecated state, compatibility ranges, and full provenance.

One successful episode does not automatically grant high-confidence permanent policy. Promotion policy may allow a low-confidence candidate to be tried, but activation and confidence must be explicit and reversible.

## 4. Retrieval

Retrieval order:

```text
user/account isolation
-> application identity
-> goal family
-> current Stage/objective
-> semantic scene and anchors
-> application/UI compatibility
-> device/orientation compatibility
-> confidence, support, failures, recency
```

The orchestrator/operator receives a bounded packet:

- relevant successful actions and expected transitions;
- actions confirmed wrong in the same scene/objective;
- recoveries confirmed effective;
- applicability and confidence;
- evidence provenance identifiers;
- explicit instruction that the current observation outranks memory.

The runtime records which experience was retrieved, which was actually used, and its new observed result.

## 5. Goal completion verification

Before a GoalRun can become `COMPLETED` or create a successful goal-level candidate:

1. load the original user goal and current GoalSpecification revision;
2. load the frozen success-criteria checklist;
3. inspect only verified facts and admissible external outcomes;
4. check that every required item is covered;
5. report completed, partial, failed, waiting, or uncertain;
6. store the evidence mapping.

Planner-generated Stage completion is input to this verifier, not authority over it.

This gate prevents the current defect where a plan containing only “launch the game” can satisfy a “finish daily tasks” request and contaminate memory.

## 6. Immediate and delayed outcomes

Phone UI work often has immediate evidence. Social interaction, scheduled work, approvals, delivery, and other external goals may have delayed evidence.

A confirmed send proves only the send. Later inbound, no-response proof, user rating, remote status, or another event settles the delayed outcome. The same transition/episode lineage must be retained until settlement; a timeout cannot blindly repeat a physical send.

A long-lived GoalRun is a durable event-driven supervisor, not one unbounded action episode. Each wake/cycle has finite action, time, retry, reflection, and recovery budgets; between cycles it waits for an event or a scheduled backoff. Candidate milestones are recorded and notified while the confirmed continuous policy keeps the goal active until explicit user stop/takeover/revision. A real gate moves the goal to a resumable wait instead of spinning or inventing progress.

The current Soul reply-learning lineage is a useful source for this generalized delayed-outcome mechanism.

## 7. Open exploration in development v1

In `development_open` mode:

- the generic learner does not block an action based only on application vocabulary;
- the local orchestrator may explore supported device actions;
- results, failures, and recoveries remain evidence-gated;
- finite action/time/reflection budgets remain;
- the user can stop or take over immediately;
- external missing facts produce waiting, not invented completion;
- every episode is scoped to an explicitly authorized test device/account.

The old `stzb-tutorial-v1` remains a compatibility profile for its old API. Its keyword restrictions do not define the universal goal runtime.

## 8. Cold/warm evaluation

Memory existence is not learning evidence. A repeat-run experiment must compare behavior.

Minimum controlled set:

- at least three initial scenes;
- modal present/absent;
- a minor anchor position variation;
- one recoverable wrong-page transition;
- one known no-effect action;
- identical or explicitly comparable success criteria.

Procedure:

1. run a cold baseline with the target experience scope disabled or empty;
2. preserve only evidence-eligible candidates;
3. reset the application to a comparable state;
4. run warm trials;
5. record retrieved and used experience per action;
6. compare completion, actions, wrong scenes, no-progress events, recovery, time, and intervention;
7. introduce a stale or contradictory experience and verify degradation/rollback.

Initial exit targets for a supported fixture:

- false completion claims: `0`;
- promoted experience with complete provenance: `100%`;
- repeated use of an evidence-confirmed same-scene wrong action: below `5%`;
- known wrong-scene automatic recovery: at least `90%`;
- no step-by-step human correction in supported warm scenarios;
- warm completion rate improves by at least 20 percentage points, or action count falls at least 30% without reducing completion rate.

Real STZB daily evidence requires observations across actual dates or resettable authorized fixtures. Re-running an already-completed daily state on the same day is not evidence of repeated daily completion.

## 9. Anti-contamination requirements

- A false or uncertain episode cannot become a successful policy.
- User, account, application, and goal-family scopes cannot leak into each other.
- Personal conversation content cannot become cross-person generic memory.
- UI/version drift lowers compatibility until reconfirmed.
- Negative evidence lowers confidence; it does not erase historical provenance.
- A regressed PolicyRevision can roll back without deleting episodes.
- Legacy SkillMemory is imported as untrusted hint until its goal coverage is revalidated.
- Raw model reasoning is not experience evidence.

## 10. No weight training in first implementation

Qwen and GUI-Owl weights remain unchanged. The first useful learning product is an evidence-backed external policy and memory system. Offline training, LoRA, dataset review, and model deployment are separate future capabilities and cannot execute phone actions themselves.
