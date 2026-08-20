"""Phase 7 cutover drill: live HTTP checks against the running console.

Usage: python cutover_drill_http_check.py <drain|kernel|legacy>

Runs the runbook §5 verification checks against the console currently
running in the given mode (see PHASE_7_ROLLBACK_RUNBOOK.md). Prints one
evidence line per check. Exits 0 when every check matches the expected
behavior for the given mode. Idempotent: the only POSTs sent are the ones
the mode gate must reject (valid payload, rejected with 403 before any
device work); in legacy mode POST /tasks is deliberately skipped to avoid
starting a real device task (documented deviation D-1 in the drill report).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4310/api/v1"
HEADER = {"X-AI-Game-Client": "console-v1"}


def _ci(headers: dict) -> dict:
    return {key.lower(): value for key, value in headers.items()}


def get(path: str) -> tuple[int, dict, dict]:
    request = urllib.request.Request(BASE + path, headers=HEADER)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body, _ci(dict(response.headers))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return error.code, body, _ci(dict(error.headers))


def post(path: str, payload: dict) -> tuple[int, dict, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, headers={**HEADER, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body, _ci(dict(response.headers))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return error.code, body, _ci(dict(error.headers))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "drain"
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        line = f"{name}: {'OK' if ok else 'FAIL'} {detail}"
        print(line)
        if not ok:
            failures.append(name)

    status, body, _ = get("/runtime/mode")
    check("GET /runtime/mode status", status == 200, f"status={status}")
    expected_flags = {
        "drain": ("draining", False, False, True),
        "kernel": ("kernel_active", False, True, False),
        "legacy": ("legacy", True, False, False),
    }[mode]
    check("mode field", body.get("mode") == expected_flags[0], f"mode={body.get('mode')}")
    check("legacy_writable", body.get("legacy_writable") is expected_flags[1],
          f"legacy_writable={body.get('legacy_writable')}")
    check("kernel_active", body.get("kernel_active") is expected_flags[2],
          f"kernel_active={body.get('kernel_active')}")
    check("draining", body.get("draining") is expected_flags[3],
          f"draining={body.get('draining')}")
    check("active_legacy_task_count", body.get("active_legacy_task_count") == 0,
          f"active={body.get('active_legacy_task_count')} ids={body.get('active_task_ids')}")

    if mode in ("drain", "kernel"):
        status, body, _ = post(
            "/tasks",
            {"goal": "drill: must be rejected", "client_request_id": "drill-1"},
        )
        check("POST /tasks rejected", status == 403,
              f"status={status} body={json.dumps(body, ensure_ascii=False)}")
    else:
        # legacy: the gate must be OPEN. We deliberately do not create a real
        # device task; the 202 path is covered by the automated matrix.
        print("POST /tasks: SKIPPED (legacy gate-open verified via mode flags; "
              "202 matrix covered by test_legacy_cutover.py to avoid a real device task)")

    if mode == "kernel":
        status, body, _ = get("/tasks")
        items = body.get("items", [])
        sample = items[0]["id"] if items else None
        check("GET /tasks has sample", sample is not None, f"count={body.get('count')}")
        if sample:
            status, body, _ = post(
                f"/tasks/{sample}/stop",
                {"client_request_id": "drill-3"},
            )
            check("POST /tasks/{id}/stop rejected", status == 403,
                  f"status={status} body={json.dumps(body, ensure_ascii=False)}")
            status, body, _ = post(
                f"/tasks/{sample}/inputs",
                {"content": "drill", "client_request_id": "drill-2"},
            )
            check("POST /tasks/{id}/inputs rejected", status == 403,
                  f"status={status} body={json.dumps(body, ensure_ascii=False)}")
        status, body, _ = get(f"/tasks/{sample}" if sample else "/tasks")
        check("GET /tasks/{id} readable", status == 200, f"status={status}")

    status, body, _ = get("/tasks")
    check("GET /tasks readable", status == 200, f"status={status} count={body.get('count')}")

    status, body, headers = get("/events")
    check("GET /events readable", status == 200, f"status={status}")
    check("GET /events deprecated", str(headers.get("deprecation", "")).lower() == "true",
          f"Deprecation={headers.get('deprecation')}")

    if failures:
        print(f"RESULT: FAIL ({len(failures)} failed: {', '.join(failures)})")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
