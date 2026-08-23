# Universal Agent work orders

These work orders implement `../06_IMPLEMENTATION_ROADMAP.md` in order.

| Order | Depends on |
|---|---|
| `U1_UNIVERSAL_GOAL_CONTRACT.md` | U0 and user implementation authorization |
| `U2_ONE_SURFACE_AND_PREFLIGHT.md` | U1 |
| `U3_QWEN_AND_GOAL_VERIFICATION.md` | U2 or an explicit advancement decision; confirmed D11/D15 |
| `U4_EXPERIENCE_LEDGER.md` | U3 |
| `U5_STZB_DAILY_LEARNING.md` | U4 |
| `U6_KERNEL_AUTONOMOUS_CANARY.md` | U5 or the advancement override recorded in D16 |
| `U7_REAL_KERNEL_CUTOVER.md` | U6 |
| `U8_LONG_LIVED_ROUTING.md` | U7 and confirmed decision D14 |
| `U9_PRODUCTION_HARDENING.md` | U8 and confirmed decision D12 |

The aggregate milestone status table is maintained once in
`../06_IMPLEMENTATION_ROADMAP.md`; each work order retains its own delivery
evidence. The single active-order pointer is maintained in `../00_INDEX.md`.

[constraint-source: USER_DECISION; ref: current execution-state instruction 2026-08-23]

Do not infer activation merely because an order exists or a milestone is
incomplete.

Every order uses `../08_AI_EXECUTION_PROTOCOL.md` and `../07_ACCEPTANCE_AND_EVIDENCE.md`.
