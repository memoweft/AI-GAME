# U5 — STZB daily repeat-run learning vertical

## STATUS

`PARTIAL`

Roadmap disposition: `CLOSED_NON_BLOCKING` on 2026-08-23 by explicit owner
direction. This preserves every missing acceptance fact below and forbids a U5
`DONE` or production STZB claim, while moving further STZB-specific hardening
to a stabilization backlog so U6 can proceed.

## BUSINESS OUTCOME

The user says “帮我把率土之滨今天的每日任务做完”. The platform discovers the current daily checklist, operates the authorized device in open-development mode, verifies each outcome, recovers from mistakes, records experience, and performs measurably better on comparable later runs.

## BASELINE FACTS AT ACTIVATION

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

At this evidence snapshot, U6 was not allowed to start under the ordinary
roadmap advancement rule and the next work was a U5 continuation focused on
robust complete-list discovery and a fresh legitimate daily state. D16 later
authorized bounded U6 advancement without relabeling these U5 gaps.

## 2026-08-21 CONTINUATION EVIDENCE

- The role prompts and a deterministic verdict guard now distinguish the
  map-side quick task strip, chapter/reputation/affairs pages, and a page with
  explicit daily/today/activity identity. A generic `名望 / 主要事宜 / 事务`
  page cannot satisfy a daily-list subgoal; a negative confirmation that such
  a page is not daily remains satisfiable.
- Executor history now carries semantic target descriptions and verifier
  evidence. Reflection preserves the unfinished plan tail and is explicitly
  directed back to visible main-navigation activity/daily surfaces after one
  task surface is exhausted. Malformed forced-tool responses receive at most
  two bounded repair attempts; network timeout remains terminal and is not
  replayed.
- Known STZB daily goals freeze a deterministic three-criterion contract before
  binding: discover the complete current-day checklist, complete or honestly
  block every frozen item, and independently reread the same checklist. Other
  goal families continue to use model-generated specifications.
- Goal list/detail projection no longer starts checklist vision extraction for
  failed, stopped, or uncertain runs. Checklist extraction is restricted to
  the candidate-completion gate or its explicit completion retry. The observed
  ten-goal list latency returned from repeated timeout to 158 ms after the
  current-source restart.
- Experience orientation begins as `unknown` until a real frame establishes
  dimensions; the old hard-coded portrait claim is no longer emitted for the
  landscape MuMu game session.
- Current-source MuMu navigation reached the independent `精彩活动` surface. A
  fresh persisted frame visibly showed `登录奖励 / 每天登录领取丰厚奖励` and
  `心愿征程 / 每日招募可获额外心愿积分`. This proves a real route to daily-labelled
  activity cards, not a complete daily-task checklist.
- GoalRun `c2189755-ce26-4c9f-95af-6626e7159ce5` bound task
  `7c80e9c1-b9c7-4b82-8fea-bea0df26cf84`. Four verified actions returned to
  main navigation and rejected the chapter, `主要事宜`, and `事务 0/15` pages as
  non-daily. It executed no occupation or warehouse-upgrade progression. The
  recovery request then ended `FAILED` with `mobile_role_unavailable`; no
  unknown action was replayed.
- A second current-source GoalRun
  `853eb57a-c295-4681-a147-c7b74efd7ac9` bound task
  `ed2c9f30-7cc9-4d40-8aba-480fc2582dda` and failed during initial planning
  after the bounded 180-second role timeout, with zero device actions.
- The machine-local Qwen launcher now explicitly passes `--no-cache-prompt`
  when configured false and fixes the dynamic vision budget at 512 tokens. A
  same-frame quality check completed in 40.96 seconds and correctly read both
  daily-labelled cards; the immediately following identical request timed out
  at 180.04 seconds. The server still logged non-consecutive-token warnings, so
  this is evidence of unresolved latency variance, not a stable runtime gate.

Focused automated verification passed 53 tests for mobile composition, tool
roles, goal specification, STZB guards/checklist behavior, experience
orientation, and the affected API path. The final current-source regression
passed 786 backend tests and 57 frontend tests; standalone TypeScript checking
and the production Vite build also passed.

The work order remains `PARTIAL`. The next U5 continuation must first stabilize
the selected local visual role under repeated dense-frame calls, then discover
and freeze one legitimate complete current-day checklist before any daily-item
execution or cold/warm claim. At that snapshot U6 remained disallowed; the
owner's later D16 decision closed U5 as a non-blocking stabilization track and
authorized U6 without changing these missing U5 facts.

## 2026-08-21 LATER CONTINUATION EVIDENCE

- The machine-local Qwen canary now uses a 32,768-token context, flash
  attention, quantized KV cache, and 1,024-pixel vision input. The same persisted
  dense MuMu frame produced three consecutive correct reads in 11.96, 11.37,
  and 11.54 seconds. The previous 65,536-token configuration timed out at 240
  seconds while leaving only about 260 MiB of GPU memory free. This stabilizes
  the targeted repeated-frame canary; it does not prove all end-to-end role
  calls are latency-bounded.
- A shared standard-library PNG perceptual hash now rejects animation-only
  visual changes. Real same-page distances measured 0-7 and real page/dialog
  transitions measured 22-27; the runtime threshold is 8. Three consecutive
  perceptually unchanged actions trigger bounded reflection instead of false
  progress.
- Reflection retries one semantically truncated replacement plan, retains the
  unfinished plan tail, and the latest source caps reflection output at 512
  tokens with thinking disabled. This final latency bound is automated-test
  evidence; it was added after a real thinking-enabled reflection took about
  30 seconds and has not yet been remeasured in a fresh device run.
- Discovery-only STZB goals now freeze only bounded discovery and item
  preservation criteria. Planner, executor, and completion gates prohibit
  item execution, claiming, completion, and recruiting. Normal completion goals
  retain the original execute-and-independent-reread requirements.
- Checklist schema revision 2 records a surface identity and explicit start/end
  coverage. Bounded partial views combine only within the same surface and
  daily-cycle label; a start-to-end manifest can freeze, and a distinct matching
  manifest is still required for final verification.
- Verifier semantics now distinguish a navigation/diagnostic subgoal from the
  whole-goal checklist gate, allow a proven negative daily-identity branch to
  close, and carry an outer daily-labelled activity-card identity into its
  renamed read-only detail page. Generic affairs/reputation labels alone remain
  insufficient positive daily identity.
- Real discovery GoalRun `768ade2f-7b61-4b06-8f52-4cbf3bb4a07a` confirmed
  `事务 0/15 / 暂无事务`, rejected chapter and reputation surfaces as daily
  checklists, read all 14 `登录奖励 / 俸禄` cumulative-login entries with the
  first three completed, and read `心愿征程` dates `2026-06-24` through
  `2026-11-04`, its first-four-daily-recruits rule, and today's `0/4` progress.
  Its 30 attempts contained only navigation, swipes, close controls, read-only
  details, and no-effect finishes; it never pressed `前往招募`, claim, complete,
  or another item-execution control.
- Directed follow-up GoalRun `94119d38-1f12-4f1b-bda4-e0945df838a2` ended
  `UNCERTAIN` after an accepted tap had no material visual change. The runtime
  did not replay the uncertain action. Its durable checklist remained
  `NOT_DISCOVERED`, so no manifest, completion fact, or positive learning policy
  was promoted.
- The final current-source backend regression passed 794 tests. The pre-existing
  frontend, standalone TypeScript, and production Vite evidence was not rerun
  for this backend-only continuation.

## REMAINING LATER-CONTINUATION GATES

- No real run has covered both activity-carousel boundaries, every visible
  daily-labelled detail, all task tabs, and the visible patrol/assistant path in
  one bounded evidence manifest.
- No legitimate complete current-day checklist is frozen. The real checklist
  state is still `NOT_DISCOVERED`.
- No feasible frozen checklist has been executed and independently reread, and
  no comparable real cold/warm pair has completed.
- The new 512-token no-thinking reflection bound needs a fresh real latency
  measurement; server non-consecutive-token warnings also remain observable.
- There is no packaged deployment or external-outcome evidence.

The work order remains `PARTIAL`. The next U5 continuation starts at complete
multi-surface discovery and manifest freeze; it must not begin item execution
until that manifest exists. At that snapshot U6 remained disallowed; D16 later
removed only the roadmap scheduling block.

## 2026-08-21 OPEN-DEVELOPMENT EXECUTION CONTINUATION

The owner explicitly overrode discovery-only behavior for ordinary early-test
runs and required visible recruit, occupation, upgrade, claim, and other task
actions to remain available. Current source keeps discovery-only semantics only
when the goal explicitly asks to inspect without execution. Normal STZB goals
plan execution and reread stages while retaining generic safety invariants.

- Task-panel identity diagnosis is now separately verifiable for
  `主要事宜`, `事务`, and `名望`; all three real pages closed negatively without
  being relabeled as daily.
- Both visible activity-carousel boundaries were read. Multiple details were
  opened, including `集思问策`, whose body explicitly states daily sessions at
  12:00, 16:00, and 20:00 and at most five daily rewards. The observed run was
  between sessions, so this item remained time-blocked.
- Three `七日试炼` cumulative-login rewards (days 1, 2, and 3) were actually
  claimed. The device rendered successive `获得新战法` result pages; later
  rereads showed updated score/node state. These actions are not represented as
  daily-checklist completion.
- Patrol reached `巡察次数 5/5 / 当前事件 6` and opened an event/rules tutorial,
  but event completion was not verified. Recruit, occupation, and warehouse
  upgrade did not reach verified outcomes.
- One accepted activity-card tap was followed by a black loading verification
  frame and correctly ended `UNCERTAIN` without replay. A later observation
  showed the loaded `赴汤蹈火` detail, and the continuation started from that
  new state.
- Planner and reflection recovery validation now reject presumed daily identity
  across task panel/overview/entry wording, including quoted `“任务”总览`.
  Tutorial prerequisites, Qwen `tap` aliasing, activity-body daily mechanics,
  and multi-page reward dismissal have focused regression coverage.
- Focused verification passed 53 tests; the final backend regression passed
  800 tests.

Later real-device continuation evidence:

- One recruit was executed. A subsequent visible tutorial identified the newly
  obtained general as `荀彧`.
- The warehouse reached `3/20`; `仓库Lv.2建造完成` and the completed task row
  prove the requested Lv.2 outcome. The extra level is a deviation caused by a
  small visual level change initially being classified as no material change.
- Occupation remains unverified and incomplete. After one confirmed sweep, the
  selected tile showed an `弃` countdown and the independent task reread still
  showed `占领Lv.2土地 1/4`; no `2/4` result is claimed.
- Patrol was actually executed: fresh pages changed from `4/5` to `3/5` and
  then `2/5`, with final `当前事件 24`. The second result was learned only from a
  later fresh observation after the sending task stopped `UNCERTAIN`, so the
  accepted action was not replayed.
- The final patrol reread completed without another patrol action and showed no
  explicit newly claimable reward; nothing was claimed in that closure run.
- Local Qwen timed out on two GoalRuns and was recovered with its owned restart
  script. A later STZB plan was rejected by the semantic identity gate; the gate
  was not weakened. The successful final patrol reread used a narrow legacy
  MobileTask, so universal GoalRun completion is still unproved.
- A deterministic STZB numeric-verdict guard now rejects `satisfied` when an
  explicit requested ratio conflicts with the fresh visible ratio or the
  evidence explicitly says that target was not reached. Matching ratios remain
  valid. Focused regression passed 56 tests and the full backend regression
  passed 803 tests with one pre-existing deprecation warning.

This owner-authorized pre-manifest execution is a development deviation and
does not pass the canonical acceptance order. The work order remains `PARTIAL`:
the complete manifest, valid occupation, whole-set independent final reread,
comparable cold/warm pair, learned warm-run improvement, and
deployment/external-outcome evidence are still absent. Recruit, warehouse, and
patrol now have real action/result evidence, but the mixed GoalRun/MobileTask
recovery chain is not acceptance of the universal end-to-end route. The next U5
continuation was expected to close those gates before U6 could start under the
ordinary rule. D16 later removed that scheduling dependency without closing or
waiving the U5 acceptance gaps.

## 2026-08-21 TAKEOVER RECOVERY CONTINUATION

- Ordinary full-goal planning now requires discovery coverage, current-feasible
  execution or explicit blockage, and a distinct final reread. A one-stage
  identity check and combined task-tab/all-item stages receive bounded semantic
  retry rather than being accepted as a complete plan.
- Recovery validation now distinguishes “the task panel is daily” from an
  explicit result that closes/exits the exhausted task panel and seeks an
  independent daily/activity/patrol surface. The former remains rejected; the
  latter is accepted while the original unfinished plan tail is preserved.
- GoalRun `88f9dc24-a937-4899-98f0-d3697554bb92` / MobileTask
  `df199be3-dddc-4cfb-a8ff-ec6f810d998e` exercised the normal v2 ingress and
  real MuMu target but failed on its second recovery-plan validation. It did
  not falsely relabel `主要事宜` or `事务 0/15 / 暂无事务` as daily and did not
  replay an uncertain action.
- GoalRun `bcb957be-1352-4a68-be75-c64ca4a4d1a2` / MobileTask
  `ba7fe121-9451-461c-80a8-8e4477672a54` then crossed that exact boundary,
  persisted five reflections and 41 attempts, traversed the activity carousel,
  and found the visible daily-labelled candidates `登录奖励 / 每天登录领取丰厚奖励`
  and `心愿征程 / 每日招募可获额外心愿积分`.
- The later sixth recovery was not accepted: one model reply used a conditional
  `若...若没有...` branch and the other again presumed a daily page inside the
  generic task panel. This is a correct bounded failure, not grounds to weaken
  identity or unconditional-stage validation.
- Focused mobile/STZB/Qwen regression passed 52 tests. The full backend
  regression passed 805 tests with the one pre-existing Starlette/httpx
  deprecation warning.

The work order remains `PARTIAL`. Artifact and automated evidence are green for
this narrow planning/recovery slice, and real-device evidence proves the normal
GoalRun can cross the old reflection failure point. A durable complete manifest,
completion of every feasible frozen item, independent whole-set reread,
comparable cold/warm pair, attributable learning improvement, deployment, and
external outcome are still absent. At that snapshot U6 remained disallowed;
D16 later removed only the roadmap scheduling block.

## 2026-08-23 PHYSICAL DISCOVERY AND MANIFEST-PLAN CONTINUATION

- Activity-carousel coverage no longer accepts a model statement that the
  current frame is the left or right boundary. The requested terminal probe
  must swipe in the correct direction and remain materially unchanged. A
  changed frame records traversal progress, while a wrong-direction swipe or
  `finish` cannot close the boundary stage. The middle sample independently
  requires a material transition.
- All activity discovery views within the current GoalRun are normalized to
  `current-daily-cycle`. This prevents the same physical carousel walk from
  being split merely because one terminal-frame extraction returned an
  uncertain date label.
- The activity candidate filter requires positive daily/today-cycle evidence
  and canonicalizes verbose card text. It rejects nearby recharge and
  next-day material and produced exactly `心愿征程` and `登录奖励` on the real
  device.
- As soon as those candidates exist, the progress controller performs a
  revision-fenced plan replacement. Generic first/second/third-detail stages
  become exact per-candidate locate and open-detail stages before any detail
  action. Frozen-item execution and independent final reread are generated
  from the same manifest rather than guessed names.
- GoalRun `6adda863-a54b-4ac0-9e71-dcec17afed27` twice rejected an unchanged
  middle frame instead of accepting the model's `satisfied` verdict, then was
  stopped with only activity-start coverage.
- GoalRun `2d427996-af07-4443-ba9b-d2289668cce2` physically proved the left
  boundary after two changed probes and one unchanged terminal probe, captured
  a distinct middle view, and proved the right boundary after one changed and
  one unchanged terminal probe. It produced the exact two-candidate activity
  manifest and was stopped before checklist freeze or item execution.
- GoalRun `29a589f3-83f2-411c-a2f5-b6973e05ec65` persisted plan revision 2
  with those exact candidate titles. It ended `FAILED` with
  `mobile_role_invalid_response` after three invalid tool-call outputs and
  before a physical candidate-detail action.
- GoalRun `1cbecd50-6510-4af6-bb80-d0a8a743919e` separately located
  `心愿征程`, opened its detail, and persisted its daily-first-four-recruits
  mechanic with visible `0/4` state. It then located `登录奖励` and opened its
  detail with the first through fourteenth day, five accumulated login days,
  and five checked entries visible.
- The latter run was stopped because the existing numbered-login-detail guard
  did not recognize the new exact-candidate subgoal wording. Current source
  now accepts that manifest-derived wording while preserving the requirement
  for a numbered cumulative-login detail. This final correction has focused
  automated evidence only and has not yet been rerun on MuMu.
- All directed runs were safely stopped or cancelled. None froze the full
  multi-surface checklist or executed a daily item before freeze. Focused
  STZB/mobile/runtime verification passed 108 tests; the complete backend
  regression passed 837 tests with one pre-existing Starlette/httpx
  deprecation warning.

The work order remains `PARTIAL`. Activity start/middle/end and the
two candidate-specific details have durable real-run coverage, while the final
current-source `登录奖励` stage verdict still needs a fresh device run. The same
bounded run must also cover task-major, task-affairs, task-reputation, patrol,
freeze the complete manifest, handle every feasible frozen item, and
independently reread that same set. A comparable cold/warm pair, attributable
learning improvement, deployment, and external outcome remain open. At that
snapshot U6 remained disallowed; D16 later removed only the roadmap scheduling
block.
