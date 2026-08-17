from __future__ import annotations

import pytest

from ai_game_console.device_lease import DeviceExecutionLease, TargetBusyError


def test_target_lease_is_nonblocking_and_release_is_idempotent() -> None:
    lease = DeviceExecutionLease()

    first = lease.acquire("127.0.0.1:16384")
    assert first is not None
    assert lease.acquire("127.0.0.1:16384") is None
    with pytest.raises(TargetBusyError, match="target_busy"):
        lease.require("127.0.0.1:16384")

    first.release()
    first.release()
    assert first.released is True
    assert lease.is_held("127.0.0.1:16384") is False

    with lease.require("127.0.0.1:16384") as second:
        assert second.target_key == "127.0.0.1:16384"
        assert lease.is_held("127.0.0.1:16384") is True
    assert lease.is_held("127.0.0.1:16384") is False
