from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.paths import ROOT


class RecallDataStore:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        if not path.is_absolute():
            path = ROOT / path
        self.db_path = path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def upsert_session(
        self,
        *,
        session_id: str,
        mirako_session_id: str,
        recall_bot_id: str | None = None,
        meeting_provider: str | None = None,
        meeting_url: str | None = None,
        mode: str | None = None,
        bridge_url: str | None = None,
        recall_bot: dict[str, Any] | None = None,
        created_meeting: dict[str, Any] | None = None,
        closed_at: float | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recall_sessions (
                    session_id, mirako_session_id, recall_bot_id, meeting_provider,
                    meeting_url, mode, bridge_url, recall_bot_json,
                    created_meeting_json, created_at, updated_at, closed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    mirako_session_id = excluded.mirako_session_id,
                    recall_bot_id = COALESCE(excluded.recall_bot_id, recall_sessions.recall_bot_id),
                    meeting_provider = COALESCE(excluded.meeting_provider, recall_sessions.meeting_provider),
                    meeting_url = COALESCE(excluded.meeting_url, recall_sessions.meeting_url),
                    mode = COALESCE(excluded.mode, recall_sessions.mode),
                    bridge_url = COALESCE(excluded.bridge_url, recall_sessions.bridge_url),
                    recall_bot_json = COALESCE(excluded.recall_bot_json, recall_sessions.recall_bot_json),
                    created_meeting_json = COALESCE(excluded.created_meeting_json, recall_sessions.created_meeting_json),
                    updated_at = excluded.updated_at,
                    closed_at = COALESCE(excluded.closed_at, recall_sessions.closed_at)
                """,
                (
                    session_id,
                    mirako_session_id,
                    recall_bot_id,
                    meeting_provider,
                    meeting_url,
                    mode,
                    bridge_url,
                    self._json_or_none(recall_bot),
                    self._json_or_none(created_meeting),
                    now,
                    now,
                    closed_at,
                ),
            )

    def add_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any] | None,
        session_id: str | None = None,
        mirako_session_id: str | None = None,
        recall_bot_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recall_events (
                    session_id, mirako_session_id, recall_bot_id, event_type, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    mirako_session_id,
                    recall_bot_id,
                    event_type,
                    self._json_or_none(payload),
                    time.time(),
                ),
            )

    def add_meeting_record(
        self,
        *,
        session_id: str,
        mirako_session_id: str,
        recall_bot_id: str | None,
        speaker: str,
        content: str,
        start_time: float | None = None,
        end_time: float | None = None,
        participant: dict[str, Any] | None = None,
        words: list[dict[str, Any]] | None = None,
        language_code: str | None = None,
        source_event: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meeting_records (
                    session_id, mirako_session_id, recall_bot_id, speaker, content,
                    start_time, end_time, participant_json, words_json,
                    language_code, source_event, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    mirako_session_id,
                    recall_bot_id,
                    speaker,
                    content,
                    start_time,
                    end_time,
                    self._json_or_none(participant),
                    self._json_or_none(words),
                    language_code,
                    source_event,
                    self._json_or_none(payload),
                    time.time(),
                ),
            )

    def get_session_data(self, *, session_id: str | None = None, mirako_session_id: str | None = None) -> dict[str, Any] | None:
        if not session_id and not mirako_session_id:
            raise ValueError("session_id or mirako_session_id is required")
        where = "session_id = ?" if session_id else "mirako_session_id = ?"
        value = session_id or mirako_session_id
        with self._connect() as conn:
            session_row = conn.execute(
                f"SELECT * FROM recall_sessions WHERE {where} ORDER BY created_at DESC LIMIT 1",
                (value,),
            ).fetchone()
            if session_row is None:
                return None
            events = conn.execute(
                "SELECT * FROM recall_events WHERE session_id = ? OR mirako_session_id = ? ORDER BY created_at ASC, id ASC",
                (session_row["session_id"], session_row["mirako_session_id"]),
            ).fetchall()
        return {
            "session": self._session_dict(session_row),
            "events": [self._event_dict(row) for row in events],
        }

    def get_meeting_records(
        self,
        *,
        session_id: str | None = None,
        mirako_session_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if mirako_session_id:
            clauses.append("mirako_session_id = ?")
            params.append(mirako_session_id)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS count FROM meeting_records {where_sql}",
                params,
            ).fetchone()["count"]
            query = f"SELECT * FROM meeting_records {where_sql} ORDER BY start_time ASC, created_at ASC, id ASC"
            query_params = list(params)
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                query_params.extend([limit, offset])
            rows = conn.execute(query, query_params).fetchall()

        return {
            "records": [self._meeting_record_dict(row) for row in rows],
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "returned": len(rows),
            },
        }

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recall_sessions (
                    session_id TEXT PRIMARY KEY,
                    mirako_session_id TEXT NOT NULL,
                    recall_bot_id TEXT,
                    meeting_provider TEXT,
                    meeting_url TEXT,
                    mode TEXT,
                    bridge_url TEXT,
                    recall_bot_json TEXT,
                    created_meeting_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recall_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    mirako_session_id TEXT,
                    recall_bot_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meeting_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    mirako_session_id TEXT NOT NULL,
                    recall_bot_id TEXT,
                    speaker TEXT NOT NULL,
                    content TEXT NOT NULL,
                    start_time REAL,
                    end_time REAL,
                    participant_json TEXT,
                    words_json TEXT,
                    language_code TEXT,
                    source_event TEXT,
                    payload_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_sessions_mirako ON recall_sessions(mirako_session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_events_session ON recall_events(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_events_mirako ON recall_events(mirako_session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_records_session ON meeting_records(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_records_mirako ON meeting_records(mirako_session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_records_time ON meeting_records(start_time, created_at)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _json_or_none(value: dict[str, Any] | None) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _load_json(value: str | None) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except ValueError:
            return value

    def _session_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "mirako_session_id": row["mirako_session_id"],
            "recall_bot_id": row["recall_bot_id"],
            "meeting_provider": row["meeting_provider"],
            "meeting_url": row["meeting_url"],
            "mode": row["mode"],
            "bridge_url": row["bridge_url"],
            "recall_bot": self._load_json(row["recall_bot_json"]),
            "created_meeting": self._load_json(row["created_meeting_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
        }

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "mirako_session_id": row["mirako_session_id"],
            "recall_bot_id": row["recall_bot_id"],
            "event_type": row["event_type"],
            "payload": self._load_json(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def _meeting_record_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "mirako_session_id": row["mirako_session_id"],
            "recall_bot_id": row["recall_bot_id"],
            "speaker": row["speaker"],
            "content": row["content"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "participant": self._load_json(row["participant_json"]),
            "words": self._load_json(row["words_json"]),
            "language_code": row["language_code"],
            "source_event": row["source_event"],
            "payload": self._load_json(row["payload_json"]),
            "created_at": row["created_at"],
        }


recall_store = RecallDataStore(settings.recall_data_db_path)
