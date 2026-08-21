# AI-GAME repository instructions

## Canonical product specification

Before changing product behavior, architecture, public contracts, runtime composition, learning, configuration, or user experience, read these documents in order:

1. `docs/product/00_INDEX.md`
2. `docs/product/01_PRODUCT_SPEC.md`
3. `docs/product/02_CURRENT_STATE.md`
4. `docs/product/03_TARGET_ARCHITECTURE.md`
5. `docs/product/04_AUTONOMY_AND_LEARNING.md`
6. `docs/product/05_MODELS_AND_AUTO_CONFIGURATION.md`
7. `docs/product/06_IMPLEMENTATION_ROADMAP.md`
8. `docs/product/07_ACCEPTANCE_AND_EVIDENCE.md`
9. `docs/product/08_AI_EXECUTION_PROTOCOL.md`
10. `docs/product/09_DECISIONS_AND_OPEN_QUESTIONS.md`
11. The active work order under `docs/product/work-orders/`, if one is explicitly active

The documents above are the only current product and implementation authority. Current user instructions override them.

## Superseded and historical material

- `RUNTIME_KERNEL_ROADMAP_STATUS.md`, root `PHASE_*.md`, and `docs/NEW/PHASE_*.md` are historical implementation evidence. They may describe code that exists, but they do not define the current product boundary or future roadmap.
- `README.md`, `docs/console-architecture.md`, `docs/mobile-task-runtime.md`, `docs/runtime-layout.md`, and `contracts/` describe implemented behavior until it is migrated. When they conflict with `docs/product/`, treat the implemented behavior as a current-state constraint and `docs/product/` as the target.
- Never infer that the product is complete because an old roadmap says Phase 0-7 is complete.
- Never implement from a superseded roadmap or design baseline.

## Execution rules

- Preserve user work. Start with `git status --short --branch` and inspect overlapping changes before editing.
- Follow the active work order. Do not silently broaden one vertical slice into a whole-platform rewrite.
- Prefer the earliest real end-to-end break: goal ingress -> orchestration -> environment -> execution -> verification -> experience -> user result.
- Keep compatibility adapters until the replacement path has runtime evidence and rollback coverage.
- Do not claim success from source code, mocks, transport acceptance, or a model statement. Use the evidence levels in `docs/product/07_ACCEPTANCE_AND_EVIDENCE.md`.
- Keep runtime invariants even in open-development autonomy mode: one device owner, user stop, revision fences, action validation, post-action observation, no uncertain replay, bounded runaway protection, and durable evidence.
- Do not add application-specific keyword bans or fixed coordinate macros to the generic core.
- Make focused local commits only when the user or active work order authorizes commits. Never push automatically.

## Documentation discipline

- Update `docs/product/02_CURRENT_STATE.md` when a completed work order changes confirmed capability.
- Update the roadmap and work-order status only after the specified verification passes.
- Record unresolved product choices in `docs/product/09_DECISIONS_AND_OPEN_QUESTIONS.md`; do not hide them in implementation comments.
- Keep artifact, automated test, runtime, real-device acceptance, deployment, and external outcome as separate facts.

## Stage completion report

At the end of every roadmap stage or work-order delivery, the final user-facing
report must contain these four sections in this order:

1. `总路线` — show U0-U9 and the current status of every stage.
2. `完成了什么` — state the observable capability and its artifact,
   automated-test, current-source runtime, real-device, learning, and
   deployment evidence separately.
3. `还有什么没完成` — list every open acceptance gate, unavailable evidence,
   deviation, and blocker without treating silence as a pass.
4. `下一阶段是什么` — name the next work order, its business outcome, and
   whether it is allowed to start under the roadmap advancement rule.

Do not replace this structure with a tool log or a generic summary. A stage
that lacks mandatory real-device or deployment evidence must still be reported
as `PARTIAL`, `BLOCKED`, `FAILED`, or `NOT RUN` as defined by the canonical
product documents.
