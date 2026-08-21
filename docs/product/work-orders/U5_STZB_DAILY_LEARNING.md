# U5 — STZB daily repeat-run learning vertical

## STATUS

`PARTIAL`

## BUSINESS OUTCOME

The user says “帮我把率土之滨今天的每日任务做完”. The platform discovers the current daily checklist, operates the authorized device in open-development mode, verifies each outcome, recovers from mistakes, records experience, and performs measurably better on comparable later runs.

## CURRENT FACTS

- A broad MobileTask path can attempt STZB goals.
- Automatic scope recognition exists but is keyword-based.
- A narrow `stzb-tutorial-v1` LearningProfile blocks many daily actions and is not the universal path.
- Existing daily SkillMemory includes at least one false-completion contamination case and is untrusted.
- One completed STZB task does not prove daily competence or learning.

## IN SCOPE

- A normalized `stzb/daily/vNext` goal family selected by GoalSpecification, not brittle exact wording alone.
- Dynamic discovery of today's visible daily requirements and frozen checklist.
- `development_open` execution through supported phone actions without inheriting tutorial keyword bans.
- Per-item verified facts and remaining items.
- Wrong-scene, no-progress, recovery, and experience-use records.
- Independent final reread/checklist verification.
- Resettable controlled fixtures plus authorized MuMu/real-device daily runs.
- Cold/warm metrics and experience compatibility by account/application/UI/device context.
- Plain reporting of completed, partial, waiting, and uncertain items.

## OUT OF SCOPE

- Claiming general game-playing or real-time combat competence.
- Anti-cheat bypass, process/network modification, or hidden game-protocol automation.
- Declaring production safety policy complete.
- Kernel cutover.
- Faking multiple daily dates by rerunning an already-completed account state.

## DESIGN

- The old tutorial profile remains on its old API only.
- The universal path does not hard-code a task-specific coordinate or complete click script.
- Current observation outranks old experience.
- Every daily item needs an observable completion/evidence rule.
- If a task requires unavailable elapsed time or external conditions, the GoalRun becomes partial/waiting rather than inventing completion.
- Real-account/device operation must be within the user's explicitly authorized environment and preserve stop/takeover.

## EARLIEST REAL BREAK

The current system can falsely finish after launching the app. Start acceptance by proving the frozen daily checklist and final goal verifier, then observe the first wrong transition; do not start with UI polish.

## VERIFY

- Goal-family normalization variants.
- Checklist completeness and final-verifier fixtures.
- Pop-up/no-pop-up, multiple initial scene, moved anchor, wrong page, no-effect, recovery, and stale-memory matrices.
- Full regression and current-source normal launcher.
- At least one controlled cold/warm experiment meeting U4 thresholds.
- Real daily evidence over actual dates or legitimate resettable authorized state.
- Zero false “all done” claims and no step-by-step correction in supported warm cases.

## ROLLBACK

Disable the STZB goal-family binding and experience retrieval. Retain episodes/evidence and return the goal as unsupported rather than falling back to contaminated legacy memory.

## DONE WHEN

The complete evidence package in `../07_ACCEPTANCE_AND_EVIDENCE.md` passes and warm-run improvement is attributable to specific validated experience.

## NEXT

`U6_KERNEL_AUTONOMOUS_CANARY.md`.

## 2026-08-21 DELIVERY EVIDENCE

- The universal GoalSpecification normalizes STZB daily wording variants to
  `stzb/daily/vnext` and binds the compatibility runtime to
  `auto:stzb/daily/vnext` without changing the legacy v1 resolver.
- A separate append-only checklist store freezes only a visibly complete first
  checklist. Final success requires a distinct later frame with the same date
  label, exact item set, and every item visibly complete. Missing or uncertain
  items remain incomplete; they do not become verified facts.
- Checklist inspection runs after MobileTask termination so it cannot starve
  the single-resident Qwen role server. Candidate frames are restricted to
  verifier evidence that positively observed the task panel.
- Reflection recovery steps can no longer erase the original unattempted plan
  tail. Recovery prompts prohibit coordinates, fixed scripts, and conditional
  replacement stages.
- Negative experience can activate before the first full Goal success only
  from two certain, evidence-referenced failures in the exact scope. It is
  bound to semantic action plus normalized screen region; legacy over-broad
  negative policies are deprecated through a new reversible policy revision.
- The STZB family has an explicit maximum of 64 actions and eight reflections
  even when the open-development runtime uses larger global budgets.
- The controlled five-scenario fixture reports 44 cold actions versus 26 warm
  actions, zero false completions, zero repeated known-wrong actions, complete
  recovery, zero interventions, stale-policy rollback, and zero cross-scope
  leakage. This is controlled artifact/test evidence, not real-device success.
- Current-source runtime used the configured Qwen3.8 27B multimodal server and
  the authorized MuMu target. One run opened the left task panel after nine
  actions without step-by-step operator correction. It later failed to expose
  a complete daily checklist; the run was stopped after repeated exploration.
  The terminal checklist remained `NOT_DISCOVERED`, so no completion fact or
  positive complete-goal policy was promoted.

## OPEN GATES

- No real run has frozen the complete current-day daily checklist.
- No real run has completed every currently feasible item and independently
  reread the same checklist from a distinct final frame.
- No comparable real STZB cold/warm pair has both completed, so controlled
  action reduction cannot be promoted to a real learning claim.
- Actual-date/resettable-state coverage and supported warm runs with zero
  step-by-step correction remain unavailable.
- There is no deployment or external-outcome evidence.

U6 is not allowed to start under the ordinary roadmap advancement rule. The
next work is a U5 continuation focused on robust complete-list discovery and a
fresh legitimate daily state.
