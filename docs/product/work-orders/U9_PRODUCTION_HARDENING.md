# U9 — Production autonomy and broader applications

## STATUS

`NOT_STARTED`

## BUSINESS OUTCOME

AI-GAME can run continuously on explicitly approved targets with reproducible installation, upgrades, rollback, observability, data protection, configurable action policy, and honest service-level evidence.

## CONFIRMED INSTALL AUTHORITY

Decision D12 in `../09_DECISIONS_AND_OPEN_QUESTIONS.md` allows automatic handling of installed owned components. A missing install or system-security change requires one concrete plan-level confirmation, after which the full approved download/install/configure/start/verify sequence proceeds automatically.

## IN SCOPE

- Installation and dependency manifest for supported Windows/WSL/Android environment.
- Durable plan-level approval scope and resumable automatic installer/change workflow.
- Automatic start/repair within approved lifecycle ownership.
- Versioned application, model-binding, schema, and policy artifacts.
- Upgrade/rollback and backup/restore with disposable-environment proof.
- Failure injection: process crash, ADB loss, model loss, database lock/corruption copy, disk pressure, stale owner, network loss.
- Non-truncating structured logs, metrics, alerts, evidence retention, and capacity limits.
- Configurable higher-risk action policy layered over `development_open`.
- Additional app and cross-app GoalRun acceptance.
- Multi-device scheduling only if required by product use.
- Optional offline dataset/training path only under a separate authorized contract.

## OUT OF SCOPE

- Claiming universal Android/application compatibility.
- Silent installation before authority.
- Reusing one plan's approval for a materially broader component, security boundary, third-party term, or destructive replacement.
- Training directly from unreviewed private data.
- Weakening Runtime invariants for throughput.
- Treating a release artifact as installed production proof.

## DESIGN

- Every prerequisite is checked before writes in install/restore paths.
- The approved plan, scope, integrity metadata, and confirmation receipt are persisted before the first installation/security mutation; retries remain inside that exact scope.
- Production policy is versioned and configurable; it does not rewrite generic Goal semantics.
- Upgrade preserves user goals, evidence, active-policy rollback, and legacy archives.
- A disposable environment/VM is used for destructive install and restore proof.
- Real deployment reports artifact, test, runtime, device, acceptance, and rollback separately.

## VERIFY

Define a release candidate matrix for supported host/device/model bindings, then run full automated, current-source, real-device, long-run, failure-injection, fresh-install, upgrade, rollback, and restore gates. Verify that one plan confirmation completes the approved workflow without step-by-step prompts, the same GoalRun resumes, and a materially expanded plan requests a new confirmation before writes. Unavailable gates remain explicit blockers to a production claim.

## ROLLBACK

Versioned package/config/schema/policy rollback with preserved evidence and verified service/target state. Never use a source checkout alone as a data rollback.

## DONE WHEN

The declared supported deployment matrix passes all mandatory gates; one scoped confirmation completes an approved missing-install workflow automatically and resumes its GoalRun; broader plans cannot reuse that authority; and an operator can observe, upgrade, recover, and roll back without undocumented manual repair.

## NEXT

Future roadmap is created only from new evidence and user goals. U9 is not a claim of universal completion.
