# Universal Agent work orders

These work orders implement `../06_IMPLEMENTATION_ROADMAP.md` in order.

| Order | Status at creation | Depends on |
|---|---|---|
| `U1_UNIVERSAL_GOAL_CONTRACT.md` | DONE | U0 DONE; user implementation authorization |
| `U2_ONE_SURFACE_AND_PREFLIGHT.md` | PARTIAL | U1 |
| `U3_QWEN_AND_GOAL_VERIFICATION.md` | DONE | Explicit user advance with U2 rendered-browser deviation retained; confirmed D11/D15 |
| `U4_EXPERIENCE_LEDGER.md` | DONE | U3 |
| `U5_STZB_DAILY_LEARNING.md` | NOT_STARTED | U4 |
| `U6_KERNEL_AUTONOMOUS_CANARY.md` | NOT_STARTED | U5 |
| `U7_REAL_KERNEL_CUTOVER.md` | NOT_STARTED | U6 |
| `U8_LONG_LIVED_ROUTING.md` | NOT_STARTED | U7, confirmed decision D14 |
| `U9_PRODUCTION_HARDENING.md` | NOT_STARTED | U8, confirmed decision D12 |

The single active-order pointer is maintained in `../00_INDEX.md`. At package creation no implementation order is active and U1 is next; U1 becomes active only after the user asks implementation to begin. Do not infer activation merely because an order file exists.

Every order uses `../08_AI_EXECUTION_PROTOCOL.md` and `../07_ACCEPTANCE_AND_EVIDENCE.md`.
