# Runtime and ownership layout

## Windows console runtime

The console is the current runnable product boundary. It is Windows-native and
does not launch WSL, the GUI model service, MuMu, or a device during startup.
AI-GAME is the generic `ApplicationRuntime` platform; Soul is one production
Profile (`soul-reply-v1`), not the platform boundary. The browser has exactly
four first-level entries: one-sentence MobileTask, Soul, Device, and Settings.
Chat and GameLearning remain compatibility or advanced APIs/history. There is
no universal Run or natural-language router between these domains.
When an ADB executor and the already-running local GUI model are configured,
constructing `MobileTaskRuntime` performs durable recovery and starts its one
daemon coordinator. A safely recoverable task can therefore resume model/device
work before HTTP readiness; startup is not itself a readiness or acceptance
probe.

```text
F:\AI-GAME\runtime
├─ console\console.db          compatibility runs/approvals + schema-v4 chat/config
├─ console\learning.db         GameLearning ledger and PolicyMemory
├─ console\mobile-tasks.db     MobileTask state, intents and SkillMemory
├─ console\application-runtime.db generic ApplicationInstance/cycle/intent ledger
├─ console\soul-scheduler-lifecycle.db content-free matcher desired-state receipt
├─ console\soul-reply-learning.db Soul draft lineage and delayed-outcome strategy
├─ console\soul-integration.db legacy SoulIntegration compatibility data only
├─ envs\console\              isolated Python environment
├─ logs\console.out.log       console standard output
├─ logs\console.err.log       console error output
├─ run\console.pid            human-readable launcher PID
├─ run\console.state.json     verified launcher/listener identity, address, and per-start shutdown token
├─ sessions\game-learning\   LearningEpisode evidence and derived artifacts
└─ sessions\mobile-tasks\evidence\ opaque MobileTask PNG/size evidence
```

The browser UI is built under `apps\console\frontend\dist` and served by the
same loopback-only backend at `http://127.0.0.1:4310`. The launcher refuses to
replace an unknown process that already owns the port. For a launcher-managed
instance, it generates a per-start local shutdown token, stores it only in the
ignored runtime state file and child-process environment, then asks the verified
listener to exit through a loopback, token-protected shutdown request. Uvicorn
then runs the FastAPI lifespan teardown before process exit. The stop script
waits up to 90 seconds for that cleanup and uses verified `Stop-Process` only as
a fallback. It checks the executable, command line, process creation time,
persisted address, and actual listener before any fallback termination. The
persisted state lets a later terminal stop an instance that was started with a
custom port; port release is confirmed and an unrelated port owner is never
terminated.

Console readiness depends only on the Windows backend, SQLite, and the built
frontend. A missing model, planner, executor, or Android device is reported as
an optional capability state and never blocks the console itself. If the local
GUI-model route and configured ADB executor were insufficient to compose
`MobileTaskRuntime`, the read-only `MobileTaskArchive` still serves list and
inspect from `mobile-tasks.db`. Create, input, and stop return
`503 mobile_task_runtime_not_configured`; they do not accept work for a later
unconfigured worker.

Long model calls and device actions run outside SQLite write transactions. The
short acceptance transaction either creates an idle Session's Turn or appends a
revisioned user Message to its current Turn; appending schedules no second
worker. On shutdown or restart, unfinished Turns are finalized rather than
replayed, unread queued Messages become `rejected`, and the console never
automatically resends an uncertain physical action.

`console.db` uses SQLite `user_version=4`. The in-place migration adds Turn
`input_revision` and Message `client_request_id`, `content_sha256`,
`input_revision`, `delivery_status`, and `applied_at` fields and indexes.
Existing v3 user Messages become applied revision 1 with their original
`created_at` used as `applied_at`; no model or device work is replayed during
migration.

`mobile-tasks.db` is an independent schema-v2 store owned by the deep
`MobileTaskRuntime` Module. It contains durable TaskState, global content-bound
request IDs, historical TaskPlan revisions, revisioned inputs, full persisted
physical intents and ActionAttempts, Reflections, redacted-event source data,
internal `skill_scope_id`, and immutable SkillMemory versions. Its one
coordinator serializes all tasks; the production queue admits 32 outstanding
tasks, with final runaway guards of 2,048 ActionAttempts and 64 Reflections per
task. Reflection still occurs at each boundary of three consecutive
no-progress verifications.

Schema v2 migrates existing task and SkillMemory keys into the `legacy:`
namespace before adding automatic scopes; migration performs no model or device
work. An explicit public `skill_id` also uses that legacy namespace. When it is
omitted, the production resolver selects a versioned `auto:` scope from the
normalized goal: scoped 率土之滨 launch/tutorial/daily/general families or a
generic exact-goal hash. Target identity is intentionally not part of this
scope, and Soul receives no MobileTask scope because `soul-reply-v1` owns its
separate delayed-outcome reply learning.

MobileTask recovery differs from Chat recovery. Construction examines active
Tasks before the coordinator begins normal queue processing and applies a fixed
precedence: a persisted but unfinalized physical `act` intent makes the Task
terminal `uncertain` with `restart_open_intent` and is never replayed;
otherwise a stopping or cancel-requested Task becomes `stopped`; otherwise a
safe active checkpoint is returned to `queued` and resumed. Recovered work
beyond the bounded queue fails explicitly as `recovery_queue_full`.

Orderly `MobileTaskRuntime.shutdown(timeout=5)` closes the mutating Interface
and serializes with the final dispatch fence. A persisted but undispatched intent
is settled `not_sent`; an already-dispatched action finishes its settle, fresh
AFTER capture, Verification, and durable settlement before the worker exits.
The next safe checkpoint is left `queued` for constructor recovery. A timeout is
reported instead of pretending shutdown completed, and archive reads remain
available during quiescence.

The production Android Adapter opens one TaskSession and holds the shared,
process-local `DeviceExecutionLease` for that entire open session: planning,
fresh observations, one-action dispatch, verification, and Reflection. In a
normal uninterrupted run this spans the task's active execution; orderly
shutdown may close it at a safe nonterminal `queued` checkpoint, and recovery
later opens and leases a new session. Chat and GameLearning share the same lease
inside this Python process, so those three paths cannot interleave device work.
The lease does not coordinate another process, direct ADB, external tools, or
the separate dating-copilot controller.

`application-runtime.db` is the schema-v2 store for the generic deep
`ApplicationRuntime` Module. It contains application instances, content-bound
request IDs, revisioned commands, cycles, redacted observations, physical
intent phases, owner reservation/receipt projections, Outcomes, events and
optional memory candidates/versions. One coordinator serializes accepted
instances. `Input`, `Pause`, and `Stop` share the final dispatch lock with owner
reserve/dispatch, so a command is accepted either before the physical commit
fence or after the owner returns, never in between.

The Soul gateway performs dependency probes and runtime construction outside
its state lock so a blocking owner/model transport cannot hold the shutdown
fence. A separate initialization lock keeps construction single-flight, and
the closed-state fence is checked again after the probe and before publishing a
new runtime. If shutdown wins, the late candidate is bounded-shutdown and is
never exposed for work.

Recovery never dispatches an unfinished application intent again. An
`open`/`reserved`/`dispatching`/`dispatched` intent is routed to the Profile's
`ExecutionOwner.reconcile` inspect path before any new policy cycle. A confirmed
owner result may converge the cycle. `active_dispatch` remains an interruptible
GET-only wait; a direct definite-not-sent result, once inspected as
`terminal_no_replay`, closes the attempt nonterminally so a fresh observation
can re-plan without replaying it. Other unresolved evidence becomes explicit
`recovery_no_replay`, uncertainty, or failure. This generic ledger, rather than
the old `SoulIntegration` command-receipt database, is the current Soul write
path.

`soul-scheduler-lifecycle.db` is a separate content-free singleton store for
the global managed Soul matcher target. It records `requested_state`, the
currently actionable `desired_state`, source instance, hash transition ref,
monotonic generation and timestamp; it contains no identity or message body.
The complete Soul archive is reduced across all instances. Any nonterminal
`queued`/`running`/`waiting` demand—or a `stopping` instance whose pre-Stop
target was running—selects running. Otherwise any paused/pre-Stop-paused demand
selects paused. With no nonterminal instance, the newest explicit lifecycle
evidence applies, except that Stop selects stopped only after that instance is
durably `stopped`; failed/completed instances therefore preserve their latest
explicit target and cannot revive an older stopped target. Monotonic generation
and the aggregate hash prevent stale lifecycle replay from rolling it back.

The independent scheduler reconciler performs an owner GET before a necessary
idempotent PUT. It repairs owner/dating-copilot reverse restarts and uses bounded
retry while unreachable, without creating another Application instance. It can
run even when cloud reply or local-vision dependencies are offline. A cold
paused restore may converge as desired paused/effective stopped without first
starting the matcher. AI-GAME process shutdown closes local activation, the
interruptible monitor, and any loaded Application runtime. The reconciler
rechecks the closed fence after owner GET and before PUT, so shutdown never
writes external desired state `stopped` or allows a late scheduler write.
When reply dependencies are unavailable during cold recovery, the reconciler
delegates only an idle Stop settlement to the dependency-free Application core.
Worker ownership and unfinished physical intents remain untouched and retain
their pre-Stop matcher demand.

`soul-reply-learning.db` is owned by the `soul-reply-v1` Adapter. It records
hash-bound draft trials, transcript/pending-generation lineage, strategy and
model versions, owner binding, physical send proof, and later interaction
evidence. Confirmed delivery alone never changes the active reply strategy;
only a later admissible engagement or no-response result does. Before owner
reserve, a learning-trial persistence failure is retryable with no remote
material. After reserve, owner binding and send-proof persistence are
best-effort: a local database failure cannot rewrite owner delivery, suppress
the single fenced dispatch, or cause a second reserve/send. The old
`soul-integration.db` may remain on disk for compatibility history, but its POST
command ledger is not part of active orchestration.

## Android ADB executor runtime configuration

`config\executor-runtime.env` is a non-secret, local handoff for the optional
GUI executor. It contains exactly `AI_GAME_GUI_EXECUTOR_ENABLED`,
`AI_GAME_ADB_PATH`, and `AI_GAME_ADB_SERIAL`. The console reads it as a strict
`KEY=VALUE` data file; it is never evaluated as PowerShell. Explicit process
environment values take precedence. The configured path is the ADB executable
used by target-bound Adapters, and the configured serial is the default when a
task does not explicitly select a Target. They are not restricted to a MuMu
binary or a loopback emulator serial.

Read-only discovery invokes exactly `adb devices -l`; it does not start the ADB
server, connect or pair a device, approve USB debugging, call `adb shell`, or
send input. A discovered Target records readiness plus connection type
`emulator`, `usb`, or `wireless`. A ready Android Target currently advertises
`android_adb`, `screen_capture`, `touch_input`, and `ascii_text_input`.

When MobileTask explicitly names a ready Android Target, the ADB Adapter derives
a target-bound executor for that Target's `external_id` serial while preserving
the configured executable and transport settings. The TaskSession and
`DeviceExecutionLease` are then keyed to that serial for that open session. When
`target_id` is omitted, the configured default serial is used. Because
MobileTask composition no longer requires that fallback, such an API request can
be durably queued and then fail asynchronously with `executor_not_configured`;
the primary browser instead requires a ready selected Android Target. Neither
path silently redirects an open TaskSession to another discovered device;
recovery resolves the persisted Target again for its new session.

Real phones and tablets must already be authorized and visible to the selected
ADB executable. A successful discovery or transport check means only that the
connection is usable; it is not evidence that a MobileTask or application goal
ran or completed. Live task evidence must come from that task's durable state
and device observations; the task-specific record documented below is not
inferred from discovery or transport readiness.

### Optional MuMu helper

MuMu assigns the ADB port dynamically. After VM 0 is already running, refresh
the configuration with:

```powershell
.\scripts\sync-mumu-executor.ps1
```

The script runs only `mumu-cli info --vmindex 0`, requires the reported VM to
already be fully started, then performs `adb connect` and `adb -s ... get-state`
before atomically replacing the config file. It never creates, starts, stops,
clones, controls, or deletes a MuMu VM. A successful sync means the local
executor has a verified ADB transport configuration; it does **not** mean a
task was run or completed. Use `scripts\test-executor-runtime-config.ps1` for
the repeatable static check, or add `-Live` to repeat the safe live discovery
and ADB verification.

## Cloud dialogue configuration

The browser console's Settings page is the primary owner-facing configuration
surface for the OpenAI-compatible endpoint, model name and API key. The backend
protects the key with Windows DPAPI for the current Windows user, then stores
only the protected blob alongside the endpoint, model and optimistic revision
in `runtime\console\console.db`. Settings responses expose only `has_api_key`;
the key is not returned to the browser or written to messages, events, logs or
provider error payloads. There is no plaintext storage fallback.

`config\cloud-runtime.env` stores only optional non-secret endpoint/model hints,
and `AI_GAME_CLOUD_CHAT_API_KEY` is an optional process-only bootstrap secret.
These inputs seed runtime configuration only when no console-managed record
exists; once the operator saves or clears configuration through the Settings
page, that persisted revision takes precedence on later starts. Saving or
clearing hot-swaps the provider for subsequently created Turns without a
console restart. An already-running Turn, including later Messages appended to
it, retains the provider generation captured when it began.

A configured route is reported as `unknown` until a real turn or an explicit
Settings-page connection test verifies it. The ordinary runtime probe does not
send background billable requests; the explicit connection test does send one
real compatible request and may be billable.

## Why the model service is in WSL

vLLM is a Linux service. The Windows control console, screenshot capture, desktop input, emulator process management, and ADB stay native to Windows. The two sides communicate through a loopback-only OpenAI-compatible HTTP endpoint.

Keeping the model environment under `/srv/ai-game` avoids Linux virtual environments and symlink-heavy package trees on an NTFS mount. The immutable model snapshot stays under `F:\AI-GAME\runtime\models` so the Windows network stack can download it despite WSL NAT/proxy isolation and so the large artifact remains visible and manageable from Windows. vLLM accesses it read-only through `/mnt/f/AI-GAME/runtime/models`.

## Ownership rules

- `F:\AI-GAME` contains source and non-secret configuration.
- `config\executor-runtime.env` is the current local ADB executable/default-
  serial configuration. The optional MuMu helper may regenerate it when that
  emulator changes its port; USB and wireless Android serials are equally valid
  executor targets once independently authorized and discovered.
- `config\cloud-runtime.env` contains optional, non-secret startup bootstrap
  hints only; the Settings page owns ongoing cloud model changes.
- `F:\AI-GAME\runtime\envs\console` is owned by the console setup script.
- `F:\AI-GAME\runtime\console` is owned by the console backend; it may contain
  the DPAPI-protected cloud credential and its configuration revision, never a
  plaintext API key, plus independent MobileTask, GameLearning,
  ApplicationRuntime, and Soul reply-learning ledgers. `mobile-tasks.db` is
  local trusted state and is not redacted: it may
  contain owner goals/inputs, complete physical intent arguments, verifier and
  Reflection text, and opaque evidence references. Tests use separate temporary
  databases.
- `F:\AI-GAME\runtime\run\console.pid` is only a human-readable locator and is never trusted without the verified state file and live process checks.
- `/srv/ai-game/envs` is owned by the model-runtime bootstrap process.
- `F:\AI-GAME\runtime\models` contains immutable, revision-pinned model snapshots and is ignored by source control.
- `/srv/ai-game/cache` may be deleted and rebuilt only through an explicit maintenance operation.
- `/srv/ai-game/run` contains only live PID/model state.
- `F:\AI-GAME\runtime\sessions\mobile-tasks\evidence` is owned by the
  MobileTask evidence-store Implementation. It stores randomly named PNGs and
  dimension JSON and has no public HTTP download route. On every new frame it
  best-effort removes incomplete and age-expired pairs, then trims the oldest
  complete pairs to default global bounds of 256 frames, 1 GiB, and 7 days while
  retaining the newly written pair. The bounds are therefore not absolute for
  that newest pair. There is no background cleanup timer; SQLite Task history
  can outlive raw evidence.
- The Windows `runtime` directory contains per-run screenshots, traces, and
  exported evidence; it is not source.
- `F:\dating-copilot` remains separately owned and is never modified by model
  bootstrap or lifecycle scripts. It is the sole Soul device/physical-ledger
  owner. AI-GAME owns the generic application cycle and calls only its loopback
  owner Interface for observation, reserve, dispatch, inspect, and managed
  scheduler desired state; it neither opens a second Soul controller nor copies
  dating-copilot's operational database under `runtime`. Explicit Application
  Start/Pause/Resume/Stop may change the owner-exposed scheduler target, but
  console/model scripts do not start or terminate the dating-copilot process.

## Local model profile

The repository contains a revision-pinned
`mPLUG/GUI-Owl-1.5-8B-Instruct` profile. It remains independent of console
startup: local chat and Android execution require it to be running, while the
control plane, compatibility APIs, and persisted history remain available when
it is stopped. Its
memory limits are local engineering settings rather than official hardware
requirements. The model lifecycle script never terminates unrelated GPU
processes to reclaim memory.

MobileTask uses this same configured endpoint sequentially through Planner,
Executor, BEFORE summarizer, AFTER summarizer, zero-image Verifier, and
Reflection role prompts. The two summarizers each receive only their own single
frame. The final Verifier sees neither image; it compares their bounded text
facts, the AFTER obstruction signal, and the local exact-frame result. Those
roles are Adapter calls, not separately installed models or resident agent
processes, and they do not use the cloud-dialogue provider.

## Security boundary

The model server binds to WSL loopback and requires a local placeholder API
key. Screenshots are sent as base64 data URLs, so vLLM is not granted
`--allowed-local-media-path /`. The Windows GUI-Owl client additionally refuses
to send screenshot bytes to a non-loopback endpoint. Each evidence summarizer
sends one current image only; the final Verifier sends none. Older steps are
represented by text-only, content-safe action summaries.

Model output is not passed to a shell. The console accepts exactly one official
`mobile_use` JSON envelope, maps a fixed action set to typed `GuiAction` values,
and invokes ADB with an argument array and `shell=False`. Timeline records omit
raw model output, screenshot bytes, typed text and credentials.

MobileTask keeps complete intent/evidence references locally but projects less
over HTTP. Public ActionAttempts omit action arguments, coordinates/typed text,
BEFORE/AFTER observations and evidence IDs, transport receipt IDs/details, and
the internal state-advance result. Public Events omit their payload data. The
owner goal and input content, follow-up input request IDs, plan/subgoal text,
strategy and Reflection text, verifier evidence, and sanitized task detail
remain visible and must not be treated as secret storage. Create and stop
request IDs stay internal.

An accepted physical action waits one second before the AFTER capture. That
settle interval is not application-idle or success proof. The Adapter compares
dimensions and PNG bytes. For an exact same frame, it suppresses a claimed
`progress=true` only when the Verifier has not established satisfaction; an
already-satisfied static final state may remain `satisfied=true / progress=true`.
Exact identity is not failure evidence, while any byte change still needs
visible verifier evidence.
The next Executor prompt receives at most eight recent redacted fingerprints:
tap/long-press retains a coarse 4x4 region, swipe a direction, typed text becomes
`text(redacted)`, and an allowed keyevent retains only its identity. Exact
coordinates, typed content, and complete intents remain only in trusted local
state.

The production chat Android path is an open-ended but sequential
screenshot-to-one-action loop: it has no fixed action-count ceiling and does not
stop on content-based sensitive-page categories. It continues until explicit
user cancellation, a device/ADB or model failure, or the local model's
`terminate` action. A legacy `interact` model output is recorded as
`redirected` and sent back through fresh observation/replanning instead of
pausing for a user. This still is not a real-time game controller: there is no
continuous state estimator or simultaneous multi-pointer gesture transport.
Provider replies, GUI proposals, and GUI termination decisions are fenced by the
Turn input revision that produced them. A stale output is discarded and the
same worker replans. A Message accepted after the final pre-dispatch revision
check is applied at the next decision boundary; an atomic ADB input already past
that boundary cannot be recalled.
The future architecture and training/custom/sandbox-only acceptance gates are
kept separately in `docs/gameplay-readiness.md` so runtime readiness is not
confused with gameplay readiness.

Code shape and test coverage do not establish live acceptance. One supplied
real-task record exists for 率土之滨: Task
`daac81a7-1af9-47e3-9566-66e73509a0fd`, 23 ActionAttempts, scope
`auto:stzb/tutorial/v1`, completed. That evidence is task-specific and does not
establish general gameplay ability. The new `soul-reply-v1` chain still awaits
this round's live acceptance. This layout also does not assert that
Mobile-Agent is installed, that the model/device is currently reachable, or
that the discrete loop is ready for real-time game control.
