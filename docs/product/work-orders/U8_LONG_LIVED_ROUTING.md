# U8 — Multi-capability and long-lived goal routing

## STATUS

`NOT_STARTED`

## BUSINESS OUTCOME

The same goal composer can start a finite phone task or a long-lived/waiting-driven application purpose. Internal module/profile/owner selection is automatic and visible only as evidence; the long-lived purpose survives candidate notifications and continues until explicit user control changes it.

## CONFIRMED CONTINUOUS POLICY

Decision D14 in `../09_DECISIONS_AND_OPEN_QUESTIONS.md` keeps the long-lived GoalRun active until explicit user stop/takeover/revision. Candidate milestones notify and accumulate evidence but do not automatically pause or complete the goal.

## IN SCOPE

- Classify finite phone operation, long-lived/waiting-driven application, active-goal message/control, and language-only cases.
- Persist a CapabilityBindingPlan before side effects.
- Bind finite phone work to Kernel.
- Bind the first long-lived candidate to ApplicationRuntime/scheduler capability.
- Use Soul/dating-copilot as an internal external-owner adapter if its live prerequisites are available.
- Generalize delayed positive, negative, no-response, and user-feedback settlement into the Experience Service.
- Present one GoalRun projection and controls.
- Produce candidate/handoff semantics for external human outcomes.

## OUT OF SCOPE

- Guaranteeing another person's behavior or a relationship outcome.
- Duplicating a specialized owner's ADB/ledger logic.
- Exposing `profile_id` or LearningJob selection to ordinary users.
- Treating engagement rate as compatibility or user satisfaction by itself.

## DESIGN

- Classification cannot cause a physical action.
- Misclassification can be rebound only before physical commitment or through explicit safe migration.
- Specialized owner ledger remains authoritative for its physical send.
- Incoming events and no-response evidence settle delayed outcomes; a send receipt is not reward.
- Person/conversation memory is isolated; private content never becomes cross-person generic experience.
- Open-ended GoalRuns notify candidate milestones and continue automatically; each wake/cycle has bounded work and backoff, while external/configuration gates produce resumable waits.

## VERIFY

- Router structure, persistence, no-side-effect classification, and fallback tests.
- Finite goal still passes through Kernel.
- Long-lived goal waits, wakes, sends at most once, survives restart, settles delayed outcome, and presents candidate/handoff truth.
- Candidate notification does not pause the goal; an explicit stop test fences all later work, while ordinary cycles remain bounded and event-driven.
- Real live acceptance if external owner and authorized account are available; otherwise status remains partial.
- Full regression and current-source normal launcher.

## ROLLBACK

Disable the long-lived binding and leave finite Kernel goals active. Stop/reconcile the specialized owner through its authoritative API; do not start a replacement ADB worker.

## DONE WHEN

At least two materially different goal families use the same ingress and projection while preserving their real internal outcome semantics and physical ownership, and the long-lived acceptance demonstrates notify-without-pausing, bounded cycles, restart continuity, and explicit-stop fencing.

## NEXT

`U9_PRODUCTION_HARDENING.md`.
