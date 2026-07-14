from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ProductDraft


class DraftStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "drafts.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    chat_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, chat_id: int | str) -> ProductDraft | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT payload FROM drafts WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()

        if row is None:
            return None

        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        return ProductDraft.from_dict(payload) if isinstance(payload, dict) else None

    def save(self, chat_id: int | str, draft: ProductDraft) -> None:
        draft.clean()
        payload = json.dumps(
            draft.to_storage_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        updated_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as database:
            database.execute(
                """
                INSERT INTO drafts (chat_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (str(chat_id), payload, updated_at),
            )

    def delete(self, chat_id: int | str) -> None:
        with self._connect() as database:
            database.execute(
                "DELETE FROM drafts WHERE chat_id = ?",
                (str(chat_id),),
            )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=5)
        database.execute("PRAGMA busy_timeout = 5000")
        return database
