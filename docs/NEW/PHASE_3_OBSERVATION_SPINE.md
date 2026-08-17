# Phase 3 — Observation Spine & Read-Only Android Integration

执行日期：2026-08-10

状态：`IMPLEMENTATION + AUTOMATED VERIFICATION COMPLETE / LIVE SMOKE BLOCKED`

## RESULT

Phase 3 的代码与自动验证已完成，但施工单要求的真实 Android 只读 Observation Smoke Test 未完成，因此本报告不宣称 `PHASE 3 DONE`。

已交付的可验证能力：

```text
Task
  → explicit device_id
  → ObservationProviderPort
  → raw Screenshot + optional UI Tree + Device State
  → atomic Filesystem Artifact finalize
  → Observation + Task.last_observation_id + ObservationReceived
  → one SQLite transaction
  → close/reopen
  → recover the same facts and ArtifactRefs
```

New Runtime 未接入 Console startup、公共 API、Legacy Runtime、DeviceExecutionLease、GUI-Owl 或任何模型层，也未创建正式 `runtime/console/runtime.db`。

## CHANGES

新增 Kernel Observation Domain：

- `apps/console/backend/ai_game_console/runtime_kernel/observation/__init__.py`
- `apps/console/backend/ai_game_console/runtime_kernel/observation/domain.py`

扩展 Kernel application/port boundary：

- `apps/console/backend/ai_game_console/runtime_kernel/__init__.py`
- `apps/console/backend/ai_game_console/runtime_kernel/kernel.py`
- `apps/console/backend/ai_game_console/runtime_kernel/ports.py`
- `apps/console/backend/ai_game_console/runtime_kernel/task/domain.py`

新增 Infrastructure Adapters：

- `apps/console/backend/ai_game_console/runtime_adapters/artifacts/__init__.py`
- `apps/console/backend/ai_game_console/runtime_adapters/artifacts/filesystem.py`
- `apps/console/backend/ai_game_console/runtime_adapters/android/__init__.py`
- `apps/console/backend/ai_game_console/runtime_adapters/android/observation.py`

扩展 SQLite Adapter：

- `apps/console/backend/ai_game_console/runtime_adapters/sqlite/store.py`

新增/更新自动测试：

- `apps/console/tests/backend/test_runtime_kernel_observation_spine.py`
- `apps/console/tests/backend/test_runtime_kernel_persistent_spine.py`

项目根目录及已检查子目录不包含可用 Git repository，因此无法提供 branch、HEAD 或 Git diff。本阶段没有执行 reset、clean、commit 或 push。

## OBSERVATION DOMAIN

最终 committed `Observation` 保存：

- `id`
- `task_id`
- `device_id`
- `captured_at`（等于 capture window 结束时间）
- `capture_started_at`
- `capture_completed_at`
- `screenshot`
- `ui_tree`
- `device_state`
- `consistency`

Channel availability 使用 canonical：

```text
AVAILABLE / UNAVAILABLE / FAILED
```

Screenshot 是 required Channel。Kernel 只允许 `AVAILABLE + non-empty bytes + positive dimensions` 进入 Artifact/DB 提交路径；FAILED Screenshot 不写 Observation 或 Event。

UI Tree 是 optional Channel。`AVAILABLE` 时保存 ArtifactRef；`UNAVAILABLE` 或 `FAILED` 时保存明确状态与可选 error code，不伪造内容，也不阻止 Observation commit。

Device State 是 required structured fact，保存：

- foreground app/package（无法确认时为 null）
- screen size
- orientation
- keyboard state
- connection state
- channel capture time

Consistency 保存 `consistent | degraded` 及可选原因。Screenshot 尺寸与设备 screen size 不同，或 capture window 中方向事实不一致时标记 degraded；不声称各 Channel 是物理原子采样。

Observation 不包含页面摘要、模型推断、Planner 判断、成功判断或 Action Proposal。

## PROVIDER

`AndroidObservationProvider` 只接受显式 `device_id`。`adb:<serial>` 被规范化为该次命令唯一使用的 `-s <serial>`；没有“选择第一个设备”的 fallback。

只读命令集合：

```text
adb -s <serial> get-state
adb -s <serial> exec-out screencap -p
adb -s <serial> shell wm size
adb -s <serial> shell dumpsys window windows
adb -s <serial> shell dumpsys input
adb -s <serial> shell dumpsys input_method
adb -s <serial> exec-out uiautomator dump /dev/tty
```

Adapter 不包含或调用 `tap`、`swipe`、`long_press`、`input_text`、`back`、`home`、`keyevent`、`open_app`、activity launch、force-stop 或 `shell input`。

Screenshot 使用已有 `parse_png_size` 基础设施验证 PNG header/IHDR，不调用 Legacy MobileTaskRuntime。UI Tree 只从 `/dev/tty` 输出提取并解析 `<hierarchy>` XML；命令失败保存 FAILED，空/无有效 hierarchy 保存 UNAVAILABLE。Foreground、orientation 和 keyboard parser 只把命令返回的设备事实结构化，不生成页面语义。

## ARTIFACT STORE

SQLite 不保存 Screenshot/UI XML BLOB。Filesystem Adapter 保存独立文件，SQLite 保存：

- portable relative reference
- content type
- byte size
- SHA-256

写入顺序：

```text
write same-directory temp file
→ flush + fsync
→ atomic no-overwrite hard-link finalize
→ remove temp name
```

No-overwrite finalize 保证同名 Observation Artifact 不能覆盖已提交历史。测试使用 `tmp_path/runtime.db` 和 `tmp_path/observations/`；没有激活正式 Artifact Root。

## SCHEMA MIGRATION

Runtime schema revision 从 Phase 2 revision 1 升为 Phase 3 revision 2。

初始化流程不是每次无条件运行全部 `CREATE TABLE IF NOT EXISTS`：

1. 空数据库显式建立 Phase 2 revision 1；
2. 读取 `runtime_schema` 的 MAX revision；
3. 只在 revision 1 时于 write transaction 内执行 `1 → 2` migration；
4. 新增 `runtime_observations` 与 task/capture index；
5. 写入 revision 2 与 `PRAGMA user_version=2`；
6. revision 2 重开时不重复 migration；
7. 比当前实现更新的 revision fail closed。

迁移测试用真实 Phase 2 DDL 预建 Task、ACTIVE Stage 和 RuntimeEvent，升级后确认旧事实、sequence 和 Stage 不变，新 Observation 可写，重复打开不重复 revision 2。

## TRANSACTION

成功 capture 的一致性顺序：

```text
capture raw channels
→ finalize Artifact(s)
→ BEGIN IMMEDIATE
→ insert runtime_observations
→ update Task.last_observation_id
→ append canonical ObservationReceived with next task-local sequence
→ COMMIT
```

Observation/Event payload 只保存 observation id、device id、channel availability、capture window 和 consistency，不复制 Screenshot 或 UI XML。

已知 Store port failure（`RuntimeStoreError`）证明 transaction 未提交时，Kernel best-effort 删除已 finalize Artifact。未知/不确定异常不删除 Artifact，以避免在 commit outcome 不确定时造成 `Observation → missing Artifact`；此时允许留下可识别 orphan。极端进程崩溃也可能留下 orphan。本阶段没有实现 orphan GC。

## LIVE SMOKE TEST

目标按 Phase 0 与当前配置显式固定为：

```text
device_id = adb:127.0.0.1:16384
```

2026-08-10 live preflight 结果：

- `adb devices -l`：没有 attached device；
- `adb -s 127.0.0.1:16384 get-state`：`device not found`；
- TCP `127.0.0.1:16384`：未监听；
- MuMu read-only `mumu-cli info --vmindex all`：VM 0 `is_process_started=false`、`is_android_started=false`、`error_code=0`。

因此没有执行 Screenshot、UI Tree 或 Device State Observation capture；没有创建临时 smoke DB/Artifact Root；Screenshot metadata、UI Tree availability、foreground before/after 和 orientation before/after 均无真实设备结果。

没有执行 `adb connect`、启动 MuMu、启动 Android、解锁屏幕或任何输入动作，因为这些会改变当前运行/连接/设备状态并超出只读施工许可。

结论：Android Adapter 已通过自动化 read-only command boundary 测试，但真实 Android Smoke/保护对比仍为 BLOCKED，不能作为 Phase 3 runtime acceptance。

## VERIFICATION

### New Runtime Phase 3 / Persistent Spine tests

命令：

```powershell
cd F:\AI-GAME\apps\console\backend
& '..\..\..\runtime\envs\console\Scripts\python.exe' `
  -m pytest `
  '..\tests\backend\test_runtime_kernel_persistent_spine.py' `
  '..\tests\backend\test_runtime_kernel_observation_spine.py' -q
```

结果：

```text
24 passed, 1 warning in 1.75s
```

覆盖 Full Observation persistence、optional UI Tree、required Screenshot、event sequence/reopen、task isolation、artifact integrity、immutable no-overwrite、DB failure cleanup、provider failure、restart recovery、Phase 2 → Phase 3 migration、Android read-only command allowlist 与 Kernel dependency boundary。

### 完整 backend regression

命令：

```powershell
cd F:\AI-GAME\apps\console\backend
& '..\..\..\runtime\envs\console\Scripts\python.exe' `
  -m pytest '..\tests\backend' -q
```

结果：

```text
451 passed, 1 warning in 65.90s
```

Phase 2 基线 438 项没有回归；新增 13 项净测试全部通过。唯一 warning 是既有 FastAPI TestClient 的 Starlette/httpx 弃用提示。

前端未修改，按施工单未重复执行前端测试。

## RUNTIME PROTECTION

完成实现和自动测试后的只读核验：

- Console：原 listener PID `35212` 仍在，`/api/v1/health` 返回 `status=ok`、`database=ready`；
- GUI-Owl：原 PID `35108` 仍监听 4243，带既有本地认证的 `/v1/models` probe 成功；
- Legacy `GET /api/v1/tasks`：仍可访问；没有创建 Legacy MobileTask；
- Android：显式目标停机，未执行 capture，也未执行任何输入、连接或启动操作；
- `F:\AI-GAME\runtime\console\runtime.db`：仍不存在；
- Console 未停止或重启；
- GUI-Owl 未停止或重启；
- 未注册新 API、未修改前端、未改变 DeviceExecutionLease。

在线 Console 的 `database=ready` 指现有 Legacy/Console 数据库，不代表 New Runtime 正式 `runtime.db` 已激活。

## BOUNDARY

仍未实现或接线：

- Action / ActionExecution / ADB input；
- Verification / Verify-Commit；
- Planner / Operator / Language / Model Router / GUI-Owl integration；
- Gateway / messages / controls / events / SSE / observations endpoint；
- Recovery / Loop Detection / Facts；
- Checkpoint；
- Context Builder；
- Chat Workbench；
- Hermes / 微信 / Soul migration；
- DeviceExecutionLease cutover；
- Legacy draining/disable；
- Console startup activation 与正式 runtime.db。

## DEVIATIONS

1. Phase 2 实际已保留 `StageCreated` canonical implementation event，因此正常 Kernel Stage 流的 Observation sequence 是 `TaskCreated=1, StageCreated=2, StageStarted=3, ObservationReceived=4`。施工单的 sequence 专项测试例按其明确示例使用已存在的 `TaskCreated=1, StageStarted=2` events，验证 Observation 为 3、重开后为 4；没有删除或改写 Phase 2 的 `StageCreated` 语义。
2. 真实 Android Smoke 未执行，原因是固定目标当前停机。此偏差使 Phase 3 未达到 DONE WHEN。
3. Channel availability 是 Phase 3 对 Phase 1 frozen fields 的工程细化：保留 frozen Screenshot/UI Tree/Device State/Consistency 字段，同时消除 null 对“未采集/不可用/失败”的歧义。

## OPEN FINDINGS

1. Owner 需在后续明确窗口自行启动并连接 Phase 0 固定 MuMu target；随后只需重跑一次临时 Store 的 read-only smoke 和前后 state comparison。当前施工单未授权 Codex 启动或连接设备。
2. `uiautomator dump /dev/tty` 已通过自动化命令边界与 XML parsing 测试，但由于目标停机，尚无该 Android 15 实例上的真实 availability/side-effect 证据。
3. Cross-store orphan GC 仍未实现，符合本阶段范围；当前设计优先保证数据库绝不主动提交 missing Artifact reference。

## STATUS

```text
PHASE 3 NOT DONE — LIVE ANDROID SMOKE BLOCKED — STOP
```
