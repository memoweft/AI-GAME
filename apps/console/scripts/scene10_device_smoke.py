"""场景 10 真机冒烟: 真实 ADB 设备路径端到端验证。

对应 docs/NEW/PHASE_5_WEEK_7_E2E_VERIFICATION.md 剩余风险表中的
"真实设备路径未集成验证"。在 mumu 模拟器(或任意真机)上验证:

1. AdbTargetDiscovery      -- 后端自带发现层能列出真实设备
2. AndroidObservationProvider -- 真实截图 + 设备状态 + UI 树
3. AdbActionExecutor        -- home / tap / swipe / back 真实执行
4. Gateway E2E (Phase 7 接线) -- 生产组合在真机上:
   设备 AVAILABLE -> Kernel Lease 持有 -> IN_USE + POST /tasks 409
   DEVICE_NOT_AVAILABLE -> 释放后回到 AVAILABLE

用法 (在 apps/console/backend 下):
    uv run python ../scripts/scene10_device_smoke.py --adb <adb.exe> --serial emulator-5554
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "console" / "backend"))

from ai_game_console.api import create_app  # noqa: E402
from ai_game_console.config import Settings  # noqa: E402
from ai_game_console.gateway_api import build_gateway_composition  # noqa: E402
from ai_game_console.runtime_adapters.adb_executor import AdbActionExecutor  # noqa: E402
from ai_game_console.runtime_adapters.android import (  # noqa: E402
    AdbDeviceRegistry,
    AndroidObservationProvider,
)
from ai_game_console.discovery import AdbTargetDiscovery  # noqa: E402
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore  # noqa: E402
from ai_game_console.runtime_kernel import RuntimeKernel, TaskSource  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    suffix = f"  [{detail}]" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    if not condition:
        raise SmokeFailure(f"{label} {detail}".strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True, help="adb 可执行文件路径")
    parser.add_argument("--serial", default="emulator-5554")
    parser.add_argument(
        "--screenshot-dir",
        default=str(PROJECT_ROOT / "runtime" / "live-acceptance"),
    )
    args = parser.parse_args()

    adb_path = Path(args.adb)
    if not adb_path.is_file():
        print(f"adb 不存在: {adb_path}")
        return 2
    serial = args.serial
    device_id = f"adb:{serial}"
    data_dir = Path(tempfile.mkdtemp(prefix="scene10-smoke-"))

    # -- 1. 发现层 ----------------------------------------------------------
    print(f"\n== 1. AdbTargetDiscovery ({device_id}) ==")
    discovery = AdbTargetDiscovery(adb_path=adb_path)
    result = discovery.discover()
    print(f"  status={result.status} adb_path={result.adb_path}")
    check(result.status == "ready", "discover() status=ready", result.message)
    listed = [d.serial for d in result.devices]
    print(f"  devices={listed}")
    check(serial in listed, f"模拟器 {serial} 在设备列表中")

    # -- 2. 观察层 ----------------------------------------------------------
    print(f"\n== 2. AndroidObservationProvider ==")
    provider = AndroidObservationProvider(adb_path=adb_path)
    observation = provider.capture(device_id)
    shot = observation.screenshot
    print(
        f"  screenshot={len(shot.content)} bytes "
        f"{shot.width}x{shot.height} consistency={observation.consistency.status.value}"
    )
    check(shot.width > 0 and shot.height > 0, "截图尺寸有效")
    state = observation.device_state
    print(f"  screen_size={state.screen_size} orientation={state.orientation.value}")
    check(
        state.screen_size == (shot.width, shot.height),
        "设备状态屏幕尺寸与截图一致",
    )
    screenshot_dir = Path(args.screenshot_dir)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / f"scene10-smoke-{serial}.png"
    screenshot_path.write_bytes(shot.content)
    print(f"  截图已保存: {screenshot_path}")

    # -- 3. 执行层 ----------------------------------------------------------
    print(f"\n== 3. AdbActionExecutor ==")
    executor = AdbActionExecutor(adb_path, timeout_seconds=10.0)
    cx, cy = shot.width // 2, shot.height // 2
    steps = [
        ("home", lambda: executor.execute_home(device_id)),
        (
            f"tap({cx},{cy})",
            lambda: executor.execute_tap(device_id, cx, cy),
        ),
        (
            f"swipe({cx},{cy + 150} -> {cx},{cy - 150},300ms)",
            lambda: executor.execute_swipe(
                device_id, cx, cy + 150, cx, cy - 150, duration_ms=300
            ),
        ),
        ("back", lambda: executor.execute_back(device_id)),
    ]
    for label, action in steps:
        outcome = action()
        check(
            outcome.accepted,
            label,
            outcome.error.message if outcome.error else f"code={outcome.adapter_code}",
        )

    # -- 4. Gateway E2E (Phase 7 切流接线, 真实 adb) -------------------------
    print(f"\n== 4. Gateway E2E (生产组合 + 真实设备) ==")
    settings = Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        database_path=data_dir / "console.db",
        frontend_dist=PROJECT_ROOT
        / "apps"
        / "console"
        / "frontend"
        / "dist",
        adb_path=str(adb_path),
        adb_serial=serial,
        runtime_mode="kernel_active",
    )
    kernel_store = SQLiteRuntimeStore(data_dir / "runtime" / "smoke.db")
    kernel = RuntimeKernel(kernel_store)
    composition = build_gateway_composition(settings=settings, kernel=kernel)
    app = create_app(settings=settings, gateway=composition)

    try:
        with TestClient(app) as client:
            payload = client.get("/api/v1/devices").json()
            items = {d["id"]: d["availability"] for d in payload["items"]}
            print(f"  devices={items}")
            # 契约 §4: HTTP availability 为小写 (available/in_use/unavailable)
            check(items.get(device_id) == "available", "Lease 前设备 available")

            # Kernel 持有该设备的 Lease (Phase 7 独占的源头)
            task = kernel.create_task(
                goal="scene10 smoke lease",
                source=TaskSource(
                    client_id="scene10-smoke",
                    conversation_id=f"conversation-{uuid.uuid4()}",
                    initial_message_id="message-smoke",
                ),
                device_id=device_id,
            )
            lease = kernel_store.acquire_lease(
                device_id=device_id,
                task_id=task.id,
                holder_process_id=str(os.getpid()),
                ttl_seconds=3600,
                lease_id=str(uuid.uuid4()),
                acquired_at=kernel._clock(),
                deadline_seconds=3900,
            )

            items = {
                d["id"]: d["availability"]
                for d in client.get("/api/v1/devices").json()["items"]
            }
            print(f"  after lease: {items}")
            check(
                items.get(device_id) == "in_use",
                "Lease 持有后设备目录显示 in_use",
            )
            check(
                kernel.active_leased_device_ids() == {device_id},
                "kernel.active_leased_device_ids() 返回该设备",
            )

            response = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "should be rejected",
                    "device_id": device_id,
                    "conversation_id": f"conversation-{uuid.uuid4()}",
                    "message_id": "message-reject",
                },
                headers={
                    "X-AI-Game-Client": "console-v1",
                    "X-Client-Id": "scene10-smoke",
                    "Idempotency-Key": "scene10-smoke-reject",
                },
            )
            error = response.json().get("error", {})
            print(
                f"  POST /tasks on leased device -> "
                f"{response.status_code} {error.get('code')}"
            )
            check(response.status_code == 409, "被拒绝: HTTP 409")
            check(
                error.get("code") == "DEVICE_NOT_AVAILABLE",
                "错误码 DEVICE_NOT_AVAILABLE",
                str(error),
            )
            check(
                error.get("retryable") is True,
                "retryable=true",
            )

            kernel_store.release_lease(lease.id)
            items = {
                d["id"]: d["availability"]
                for d in client.get("/api/v1/devices").json()["items"]
            }
            print(f"  after release: {items}")
            check(
                items.get(device_id) == "available",
                "释放 Lease 后设备回到 available",
            )
    finally:
        kernel.close()
        composition.store.close()

    print(f"\n场景 10 冒烟全部通过: {serial} (data_dir={data_dir})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as failure:
        print(f"\n场景 10 冒烟失败: {failure}")
        raise SystemExit(1)
