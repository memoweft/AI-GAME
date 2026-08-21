# AI-GAME vNext canonical specification

> Status: **CURRENT AND AUTHORITATIVE**  
> Effective date: 2026-08-20  
> Product: Universal Local Mobile AI Operator  
> Supersedes: the deleted `docs/NEW/docs/00_README.md` through `20_OPEN_QUESTIONS_AND_NON_GOALS.md` design baseline and every older product boundary or roadmap that conflicts with this package

## 1. Authority

This directory is the single source of truth for what AI-GAME is intended to become and how an implementation AI should advance it.

Priority order:

1. The user's current explicit instruction.
2. Root `AGENTS.md`.
3. This `docs/product/` package.
4. The active work order.
5. Implemented API contracts and current architecture documentation, as descriptions of current behavior only.
6. Historical phase plans, acceptance reports, runbooks, and old roadmap records, as evidence only.

An old document cannot overrule this package merely because it says `FROZEN`, `DONE`, `7/7`, `10/10`, or `Phase 0-7 complete`.

## 2. One-sentence definition

AI-GAME is a local-first universal phone operator. The user states an outcome in ordinary language; AI-GAME takes responsibility for discovering and configuring the available environment, planning, operating the phone, observing and verifying effects, recovering from mistakes, retaining evidence-backed experience, and continuing until it reaches an honest terminal or waiting state.

Games, social applications, settings, shopping, logistics, content work, and future applications are target environments of the same platform. None of them defines the product.

## 3. Frozen product decisions

1. There is one primary natural-language goal entry, not a product menu of runtimes or profiles.
2. Users express outcomes. They do not select MobileTask, ApplicationRuntime, GameLearning, Soul, a model, a device serial, a skill id, or a runtime mode.
3. Internal modules and application adapters remain implementation details selected by the orchestrator.
4. The local Qwen3.8 27B deployment is the default goal orchestrator and the measured current visual phone operator. U3 selected it over GUI-Owl on identical persisted real-device frames; GUI-Owl remains a serial explicit diagnostic/rollback binding while generic contracts remain capability-based.
5. The platform learns in vNext through evidence-backed experience memory and policy revisions, not by silently fine-tuning model weights.
6. The first development version prioritizes a working autonomous loop. It does not apply broad application-specific keyword bans by default.
7. Open autonomy does not remove runtime correctness: stop, ownership, validation, verification, no uncertain replay, recovery, evidence, and runaway bounds remain mandatory.
8. An action sent is not an action verified; a task marked complete by a model is not a verified user outcome.
9. External results outside the phone's control cannot be guaranteed. The runtime must represent partial progress, waiting, candidate notification, optional takeover, and user-confirmed outcomes honestly.
10. Configuration is part of the product loop. The system automatically discovers and repairs what it safely can, and asks one plain-language question only for an unavoidable external gate.
11. A missing model/software install or operating-system security change uses one concrete plan-level confirmation; after approval, the entire approved install/configure/start/verify workflow is automatic.
12. Official acceptance proceeds settings/battery first, STZB repeat-run learning second, and a long-lived application/social goal third.
13. A long-lived matching/conversation goal continues through candidate notifications until explicit user stop/takeover/revision; every wake/cycle remains bounded and event-driven.

## 4. Document map

| Document | Purpose |
|---|---|
| `01_PRODUCT_SPEC.md` | Product purpose, user experience, scope, and behavioral contract |
| `02_CURRENT_STATE.md` | Verified current implementation, evidence, and gaps |
| `03_TARGET_ARCHITECTURE.md` | Canonical target components, ownership, data flow, and migration roles |
| `04_AUTONOMY_AND_LEARNING.md` | Observe-act-verify-recover-learn semantics and repeat-run improvement |
| `05_MODELS_AND_AUTO_CONFIGURATION.md` | Qwen/GUI-Owl responsibilities, capability routing, and zero-config target |
| `06_IMPLEMENTATION_ROADMAP.md` | Ordered vertical milestones U1-U9 and dependencies |
| `07_ACCEPTANCE_AND_EVIDENCE.md` | Test levels, business gates, real-device evidence, and honest claims |
| `08_AI_EXECUTION_PROTOCOL.md` | How implementation agents inspect, change, verify, continue, and report |
| `09_DECISIONS_AND_OPEN_QUESTIONS.md` | Frozen decisions, assumptions needing confirmation, and future choices |
| `work-orders/` | Bounded implementation instructions in roadmap order |

## 5. Reading paths

For product decisions: `01 -> 02 -> 09`.

For implementation: `02 -> 03 -> 04 -> 05 -> 06 -> 08 -> active work order`.

For review and acceptance: `01 -> 07 -> relevant work order -> current-state update`.

## 6. Historical evidence policy

Historical reports are retained when they prove a useful fact, for example a test result, a MuMu smoke run, a cutover drill, or a rollback procedure. Their conclusions do not automatically remain current. In particular:

- The old Phase 0-7 completion means the former Kernel/Gateway/Legacy-migration construction plan reached its recorded checks.
- It does not mean the universal natural-language platform described here exists.
- A local `legacy -> draining -> kernel_active -> legacy` drill is not a production deployment.
- A single completed application task is not evidence of general application competence or repeat-run learning.

## 7. Current execution pointer

```text
Current active work order: none (U4 completed)
Next planned work order: U5_STZB_DAILY_LEARNING
```

U1 completed against its specified gates. U2 remains PARTIAL only because its
real rendered primary-composer refresh/close/reopen proof is unavailable; its
managed repairs and real-device execution gates are green. On 2026-08-21 the
user explicitly instructed the roadmap to continue, overriding the ordinary
advancement hold without waiving or relabeling the U2 deviation. U3 then passed
its own automated, current-source, real-device, completion, learning-gate, and
model-comparison evidence. U4 then completed its additive experience ledger,
controlled cold/warm thresholds, and attributable real settings/battery warm
run. U5 is next but is not active until execution continues.
