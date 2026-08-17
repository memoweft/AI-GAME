# AI-GAME Domain Context

AI-GAME is a general mobile intelligent-agent console. It accepts a natural-language objective, maintains durable progress, uses a local GUI model to understand and operate the current screen, verifies effects from fresh observations, changes strategy when progress stalls, and reuses evidence-backed experience. Games, content publishing, and Soul dating automation are application capabilities of the same product; no single application defines the product boundary.

## General mobile-agent work

**MobileTask**:
A durable user objective executed through one target device. A MobileTask owns progress and recovery across many atomic GUI actions; it is not a chat message, legacy control-plane Run, or LearningJob.
_Avoid_: Run, turn, workflow instance

**TaskPlan**:
A revisioned decomposition of a MobileTask into ordered, observable Subgoals. The first implementation may ask the same GUI model to plan, execute, and verify under different role prompts; TaskPlan does not imply several resident Agent processes.
_Avoid_: Chain of thought, model transcript

**Subgoal**:
One externally observable progress checkpoint within a TaskPlan, such as opening an application section or claiming a reward. A Subgoal is complete only after verification against a fresh observation.
_Avoid_: Click, action, assumption

**TaskState**:
The durable current goal, plan revision, active Subgoal, completed and remaining Subgoals, retry and reflection counts, current phase, and terminal result of a MobileTask. Chat history alone is not TaskState.
_Avoid_: Conversation context, action history

**ActionAttempt**:
One atomic proposed device action together with its before observation, transport receipt, after observation, and verification. Transport acceptance is not action or Subgoal success.
_Avoid_: Successful click, completed step

**TaskVerifier**:
A role that compares admissible before and after observations against the active Subgoal and returns success, failure, or uncertainty with a short evidence-grounded reason. It may use the same configured GUI model under a verifier prompt.
_Avoid_: Executor acknowledgement, image changed flag

**Reflection**:
A bounded, persisted change of strategy after a failed or repeated no-progress verification. Reflection must explain what failed and alter the next approach; appending the same action again is not Reflection.
_Avoid_: Retry, hidden reasoning, log message

**SkillMemory**:
Versioned, application-scoped reusable procedure knowledge distilled only from verified Task or Learning evidence. A SkillMemory provides hints and checkpoints to a future run while the current screen remains authoritative.
_Avoid_: Macro replay, raw trajectory, model weight

**ApplicationProfile**:
A versioned binding of an application or game to its package, task vocabulary, admissible actions, verifier, skills, and optional long-running policy. `soul-dating` is one ApplicationProfile; it does not redefine MobileTask, ChatMode, Target, or AI-GAME itself.
_Avoid_: Product mode, hard-coded page

## Module boundaries

- The generic Mobile Task Runtime owns TaskState, planning roles, verification, bounded reflection, SkillMemory retrieval, cancellation, and restart recovery.
- GUI-Owl plus the Android executor remains the local visual-action core. Mobile-Agent is a reference implementation and parts library, not a required runtime dependency.
- Chat owns user dialogue, mid-turn instruction revisions, and optional delegation to a MobileTask. A Chat Turn is not the durable task itself.
- Game Learning owns evidence-backed trajectory collection and PolicyMemory promotion. It is reusable by the generic Runtime but remains distinct from ordinary task completion.
- Targets and device leases remain generic physical resources.
- Application integrations own application-specific policy and projections. The Soul integration remains an independent long-running application backed by `dating-copilot`; it is not a replacement for generic Chat, Learning, or Targets.
- Legacy Workflow, Run, and Approval records that have no executor are compatibility data, not evidence that work is executing.

AI-GAME Learning records bounded, evidence-backed attempts to improve reusable GUI policy knowledge. It exists to distinguish collected trajectories, verified outcomes, and promoted policy knowledge from model-weight training or broad game-playing capability.

## Learning work

**LearningProfile**:
A versioned scope that fixes the permitted application situation, preconditions, action and time budgets, evidence rules, outcome verifier, and policy compatibility domain for learning work.
_Avoid_: Game preset, universal skill

**LearningJob**:
An operator-visible, durable request to learn under one LearningProfile. A LearningJob owns its processing phase, terminal result, and exactly one bounded LearningEpisode in the first version.
_Avoid_: Training job, run

**LearningEpisode**:
One finite attempt to collect a sequence of GUI interactions and decide what, if anything, may be learned from them. Ending an episode does not by itself mean the objective succeeded or policy knowledge changed.
_Avoid_: Match, gameplay session, training run

**Transition**:
One ordered ledger fact connecting an observation, a proposed or transported atomic action, the following observation, and their evidence references. A Transition records what was observed and attempted; it does not infer success from transport acceptance.
_Avoid_: Step, click log

**Phase**:
The current processing position of a LearningJob, independent of its eventual result, verified outcome, and policy state.
_Avoid_: Result, outcome, status

**Result**:
The terminal learning disposition of a LearningJob: learned, not learned, failed, stopped, or stopped with unresolved physical uncertainty.
_Avoid_: Phase, outcome

## Evidence and outcomes

**Evidence**:
Locally retained, integrity-addressed material that can support or refute a claim about a Transition or episode outcome. Evidence is not the claim itself.
_Avoid_: Proof, success flag

**EvidenceGate**:
The rule that no outcome, reward, or policy promotion may become positive merely because an action was proposed, transported, or followed by a changed image.
_Avoid_: Transport check

**TransportAcceptance**:
Confirmation that the device transport accepted an atomic input request. It says nothing about application-level completion, effect, or correctness.
_Avoid_: Action success, outcome confirmation

**OutcomeVerifier**:
A profile-specific evaluator that classifies an episode only from admissible post-action evidence and explicit uncertainty rules.
_Avoid_: Success detector, model opinion

**Outcome**:
The evidence classification of an episode: unknown, confirmed success, confirmed failure, or unconfirmed. Outcome is independent of whether the job failed operationally or produced policy knowledge.
_Avoid_: Result, reward

**RewardSignal**:
A bounded learning signal derived from an evidence-gated Outcome and selected Transition facts. It is not a user score, business result, or model-weight update.
_Avoid_: Win, model loss, ground truth

## Reusable policy knowledge

**TrajectoryDistillation**:
The deterministic or model-assisted reduction of an evidence-backed Transition ledger into reusable policy knowledge. It does not update model weights.
_Avoid_: Training, fine-tuning

**PolicyMemory**:
Versioned, profile-scoped reusable knowledge distilled from eligible trajectories. A stored PolicyMemory is immutable; later changes produce a new version.
_Avoid_: Model, checkpoint, global memory

**PolicyCandidate**:
A future proposed PolicyMemory or weight-adapter version that is stored for separate evaluation but is not active for subsequent learning episodes. The first LearningJob implementation does not persist candidates.
_Avoid_: Learned policy, active memory

**Promotion**:
The evidence-gated state change that makes a new version active for its compatible scope. In the first LearningJob implementation, eligible trajectory distillation and PolicyMemory promotion are one atomic transaction; a future explicit PolicyCandidate still requires separate validation and never activates merely because it exists.
_Avoid_: Save, publish, candidate creation, unverified auto-learn

**PolicyState**:
The policy disposition of a LearningJob: unchanged, candidate, promoted, or rejected. The first implementation emits unchanged or promoted; candidate and rejected are reserved for future separately persisted candidates. PolicyState is separate from the job Result and episode Outcome.
_Avoid_: Result, outcome

## Profile scope and future training

**STZBTutorialProfile**:
The versioned, low-frequency tutorial scope for the `stzb-tutorial-v1` compatibility profile. It is a constrained evidence-collection profile, not a claim of general competence in or endorsement by the game it names.
_Avoid_: STZB player, game mastery

**TrainingJob**:
A future, explicitly authorized offline weight-training process that consumes a frozen, reviewed dataset. It is not a LearningJob and never executes device actions.
_Avoid_: Learning job, episode

**LoRAAdapter**:
A future versioned model-weight artifact produced outside a LearningEpisode and loaded only through a separately governed model adapter. It is not PolicyMemory and is absent from the first version.
_Avoid_: Policy memory, learned trajectory
