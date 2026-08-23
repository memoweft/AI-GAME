from pathlib import Path

from ai_game_console.goal_runtime import GoalService, SQLiteGoalStore


class FakeKernelCanary:
    def __init__(self) -> None:
        self.status = "running"
        self.controls: list[tuple[str, str]] = []

    def start(self, goal, client_request_id, **options):
        return self.inspect("kernel-task-1")

    def inspect(self, task_id):
        assert task_id == "kernel-task-1"
        return {
            "task_id": task_id,
            "status": self.status,
            "events": (),
            "detail": None,
            "error_code": None,
        }

    def control(self, task_id, action):
        self.controls.append((task_id, action))
        self.status = {"pause": "paused", "resume": "running", "stop": "stopped"}.get(
            action, self.status
        )
        return self.inspect(task_id)

    def send(self, task_id, content, client_request_id):
        return self.inspect(task_id)


def test_goal_service_explicitly_binds_kernel_canary_and_exposes_pause_resume(
    tmp_path: Path,
) -> None:
    kernel = FakeKernelCanary()
    service = GoalService(
        SQLiteGoalStore(tmp_path / "goals.db"),
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=kernel,
        kernel_canary_enabled=True,
        configured_serial="device-1",
    )
    created = service.create("查看设置后回桌面", "goal-key")
    assert created.binding_kind == "runtime_kernel_canary"
    assert created.bound_task_id == "kernel-task-1"
    assert created.execution_status == "RUNNING"

    paused = service.control(created.id, "pause", "pause-key")
    assert paused.control_state == "PAUSED"
    resumed = service.control(created.id, "resume", "resume-key")
    assert resumed.execution_status == "RUNNING"
    assert kernel.controls == [
        ("kernel-task-1", "pause"),
        ("kernel-task-1", "resume"),
    ]


def test_goal_service_binds_canonical_kernel_after_u7_cutover(
    tmp_path: Path,
) -> None:
    kernel = FakeKernelCanary()
    service = GoalService(
        SQLiteGoalStore(tmp_path / "goals.db"),
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=kernel,
        kernel_binding_kind="runtime_kernel",
        configured_serial="device-1",
    )

    created = service.create("查看设置后回桌面", "goal-kernel-active")

    assert created.binding_kind == "runtime_kernel"
    assert created.bound_task_id == "kernel-task-1"
    paused = service.control(created.id, "pause", "pause-kernel-active")
    assert paused.control_state == "PAUSED"
