from __future__ import annotations

from ai_game_console.execution import ActionTransportResult
from ai_game_console.runtime_adapters.adb_executor import GuiExecutorActionAdapter


class _GuiExecutor:
    serial = "device-1"

    def __init__(self) -> None:
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return ActionTransportResult(True, "accepted")


def test_gui_executor_action_adapter_binds_target_and_preserves_unicode() -> None:
    executor = _GuiExecutor()
    serials: list[str] = []
    adapter = GuiExecutorActionAdapter(
        lambda serial: serials.append(serial) or executor,
        transport_device_id_resolver=lambda device_id: (
            "adb:192.168.31.232:46387"
            if device_id == "adb:device-1"
            else device_id
        ),
    )

    text = adapter.execute_input_text("adb:device-1", "你好，继续聊聊")
    press = adapter.execute_long_press(
        "adb:device-1", 120, 240, duration_ms=800
    )

    assert text.accepted is True
    assert press.accepted is True
    assert serials == ["192.168.31.232:46387", "192.168.31.232:46387"]
    assert executor.actions[0].target_id == "adb:device-1"
    assert executor.actions[0].action == "text"
    assert executor.actions[0].text == "你好，继续聊聊"
    assert executor.actions[1].action == "long_press"
    assert executor.actions[1].duration_ms == 800
