# Local console

The console is split into two source modules:

- `backend/`: the Windows-native control-plane API and SQLite persistence;
- `frontend/`: the browser user interface served by the backend after build.

From the project root:

```powershell
.\scripts\console.ps1 setup
.\scripts\console.ps1 start
.\scripts\console.ps1 status
.\scripts\console.ps1 stop
.\scripts\console.ps1 test
```

The default address is `http://127.0.0.1:4310`. The console is intentionally
local-only and does not require the GUI model service to start.

Cloud dialogue is configured from **设置 → 云端模型配置** in the console. The
OpenAI-compatible endpoint and model are stored with the console state; the API
key is protected for the current Windows user with DPAPI and is never returned
to the browser. Save and clear take effect for subsequently created Turns
without restarting the console; a Turn already running, including later input
appended to it, retains the provider generation captured when it began. The
connection test sends one real model request and may be billable.

`POST /api/v1/chat/sessions/{session_id}/turns` always returns `202` with a
`ChatTurn` after durable acceptance. An idle Session gets a new Turn. If its
current Turn is `accepted`, `queued`, `thinking`, `planning`, or `executing`, the
new user Message joins that same Turn, increments `input_revision`, and is read
by the existing worker; no second worker is scheduled. A `stopping` Turn rejects
new input.

For user Messages, `delivery_status=queued` means durable but not yet read;
`applied` means included in a model-decision snapshot, not that a reply, Android
action, or goal succeeded; and `rejected` means stop, cancellation, failure, or
restart won before any model read it. Provider replies and Android proposals or
termination decisions are revision-fenced. Stale output is discarded and the
same worker replans. Input accepted after the final pre-dispatch revision check
affects the next decision but cannot recall an atomic input already dispatched.

The current Android executor is a bounded, sequential screenshot-to-one-action
loop for ordinary GUI work. It is not yet a real-time game controller; the
training/custom/sandbox-only target architecture and acceptance gates are in
`docs/gameplay-readiness.md`.
