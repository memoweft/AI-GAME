from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from ai_game_console.repository import SQLiteRepository


def test_sqlite_v4_migration_upgrades_existing_v1_database(tmp_path: Path) -> None:
    database = tmp_path / "console.db"
    repository = SQLiteRepository(database)
    repository.initialize()

    # Simulate a pre-chat v1 database while preserving its control-plane data.
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            DROP TABLE chat_steps;
            DROP TABLE chat_messages;
            DROP TABLE chat_turns;
            DROP TABLE chat_sessions;
            PRAGMA user_version = 1;
            """
        )

    upgraded = SQLiteRepository(database)
    upgraded.initialize()
    session = upgraded.create_chat_session(
        session_id="session-v2",
        title="迁移验证",
        mode="local_chat",
        target_id=None,
        auto_execute=False,
    )

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'chat_%'"
            )
        }
    assert version == 4
    assert tables == {"chat_sessions", "chat_turns", "chat_messages", "chat_steps"}
    assert session["id"] == "session-v2"


def test_sqlite_v4_migrates_v3_chat_data_and_backfills_input_lifecycle(
    tmp_path: Path,
) -> None:
    database = tmp_path / "console-v3.db"
    content_hash = hashlib.sha256("保留的消息".encode("utf-8")).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE chat_sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL,
                target_id TEXT, auto_execute INTEGER NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE chat_turns (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL, request_content_sha256 TEXT NOT NULL,
                mode TEXT NOT NULL, target_id TEXT, auto_execute INTEGER NOT NULL,
                status TEXT NOT NULL, reply_status TEXT, execution_status TEXT,
                step_count INTEGER NOT NULL DEFAULT 0, blocker TEXT, detail TEXT,
                error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                provider TEXT, model TEXT, execution_goal_json TEXT,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                updated_at TEXT NOT NULL, UNIQUE(session_id, client_request_id)
            );
            CREATE TABLE chat_messages (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
                role TEXT NOT NULL, content TEXT NOT NULL, provider TEXT,
                model TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE chat_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL,
                step_index INTEGER NOT NULL, state TEXT NOT NULL,
                action_type TEXT, summary TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(turn_id, step_index)
            );
            INSERT INTO chat_sessions VALUES (
                'legacy-session', '旧对话', 'local_chat', NULL, 0, 'active',
                '2026-08-08T00:00:00.000Z', '2026-08-08T00:00:02.000Z'
            );
            INSERT INTO chat_turns VALUES (
                'legacy-turn', 'legacy-session', 'legacy-request', '{content_hash}',
                'local_chat', NULL, 0, 'completed', 'completed', 'not_requested',
                0, NULL, '旧回复完成。', NULL, 0, 'legacy-provider', 'legacy-model',
                NULL, '2026-08-08T00:00:00.000Z', '2026-08-08T00:00:00.100Z',
                '2026-08-08T00:00:02.000Z', '2026-08-08T00:00:02.000Z'
            );
            INSERT INTO chat_messages VALUES (
                'legacy-user', 'legacy-session', 'legacy-turn', 'user', '保留的消息',
                NULL, NULL, '2026-08-08T00:00:00.000Z'
            );
            INSERT INTO chat_messages VALUES (
                'legacy-assistant', 'legacy-session', 'legacy-turn', 'assistant',
                '保留的回复', 'legacy-provider', 'legacy-model',
                '2026-08-08T00:00:02.000Z'
            );
            PRAGMA user_version = 3;
            """
        )

    repository = SQLiteRepository(database)
    repository.initialize()
    transcript = repository.get_chat_transcript("legacy-session")

    assert transcript is not None
    assert transcript["turns"][0]["input_revision"] == 1
    assert transcript["messages"][0] == {
        "id": "legacy-user",
        "session_id": "legacy-session",
        "turn_id": "legacy-turn",
        "role": "user",
        "content": "保留的消息",
        "client_request_id": "legacy-request",
        "content_sha256": content_hash,
        "input_revision": 1,
        "delivery_status": "applied",
        "applied_at": "2026-08-08T00:00:00.000Z",
        "provider": None,
        "model": None,
        "created_at": "2026-08-08T00:00:00.000Z",
    }
    assert transcript["messages"][1]["content"] == "保留的回复"
    assert transcript["messages"][1]["delivery_status"] is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_turn_revision_fence_and_terminal_paths_preserve_input_lifecycle(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "console.db")
    repository.initialize()
    repository.create_chat_session(
        session_id="session",
        title="修订栅栏",
        mode="local_chat",
        target_id=None,
        auto_execute=False,
    )
    first_hash = hashlib.sha256("第一条".encode("utf-8")).hexdigest()
    turn, created = repository.accept_chat_turn(
        turn_id="turn",
        message_id="message-1",
        session_id="session",
        client_request_id="request-1",
        request_content_sha256=first_hash,
        content="第一条",
    )
    assert created is True and turn is not None
    repository.begin_chat_turn("turn", "thinking")
    claimed, _ = repository.claim_chat_turn_inputs("turn")
    assert claimed["input_revision"] == 1

    second_hash = hashlib.sha256("第二条".encode("utf-8")).hexdigest()
    joined, created = repository.accept_chat_turn(
        turn_id="unused",
        message_id="message-2",
        session_id="session",
        client_request_id="request-2",
        request_content_sha256=second_hash,
        content="第二条",
    )
    assert created is False and joined is not None
    assert joined["id"] == "turn"
    assert joined["input_revision"] == 2

    stale = repository.finish_chat_turn(
        turn_id="turn",
        status="completed",
        reply_status="completed",
        execution_status="not_requested",
        blocker=None,
        detail="过期完成不得生效。",
        error_code=None,
        expected_input_revision=1,
    )
    assert stale["status"] == "thinking"
    transcript = repository.get_chat_transcript("session")
    assert transcript is not None
    assert [message["delivery_status"] for message in transcript["messages"]] == [
        "applied",
        "queued",
    ]

    current, _ = repository.claim_chat_turn_inputs("turn")
    completed = repository.finish_chat_turn(
        turn_id="turn",
        status="completed",
        reply_status="completed",
        execution_status="not_requested",
        blocker=None,
        detail="最新修订完成。",
        error_code=None,
        expected_input_revision=current["input_revision"],
    )
    assert completed["status"] == "completed"
    transcript = repository.get_chat_transcript("session")
    assert transcript is not None
    assert [message["delivery_status"] for message in transcript["messages"]] == [
        "applied",
        "applied",
    ]


def test_cancel_failure_and_restart_reject_only_queued_inputs(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "console.db")
    repository.initialize()
    for suffix in ("cancel", "failure", "restart"):
        repository.create_chat_session(
            session_id=f"session-{suffix}",
            title=suffix,
            mode="local_chat",
            target_id=None,
            auto_execute=False,
        )
        repository.accept_chat_turn(
            turn_id=f"turn-{suffix}",
            message_id=f"message-{suffix}",
            session_id=f"session-{suffix}",
            client_request_id=f"request-{suffix}",
            request_content_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
            content=suffix,
        )

    repository.request_chat_turn_cancel("turn-cancel")
    repository.finish_chat_turn(
        turn_id="turn-failure",
        status="failed",
        reply_status="failed",
        execution_status="not_requested",
        blocker="test_failure",
        detail="测试失败。",
        error_code="test_failure",
    )
    repository.recover_interrupted_chat_turns()

    for suffix in ("cancel", "failure", "restart"):
        transcript = repository.get_chat_transcript(f"session-{suffix}")
        assert transcript is not None
        assert transcript["messages"][0]["delivery_status"] == "rejected"
        assert transcript["messages"][0]["applied_at"] is None
