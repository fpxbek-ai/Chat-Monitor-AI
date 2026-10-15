"""Local SQLite store. New schema deliberately leaves the original 'messages' table intact."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3


@dataclass(frozen=True)
class MessageRecord:
    chat_id: int
    message_id: int
    author_id: int
    author_name: str
    message_text: str
    timestamp: str
    media_type: str = "text"


class MessageStore:
    def __init__(self, db_path: str = "messages.db"):
        self.db_path = str(Path(db_path))
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS monitored_messages_v3 (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'text',
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_monitored_timestamp_v3 "
                        "ON monitored_messages_v3(timestamp)")
        if os.name == "posix":
            os.chmod(self.db_path, 0o600)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def add(self, *, chat_id: int, message_id: int, author_id: int,
            author_name: str, message_text: str, timestamp: str,
            media_type: str = "text") -> bool:
        with self._connect() as con:
            result = con.execute("""
                INSERT OR IGNORE INTO monitored_messages_v3
                (chat_id, message_id, author_id, author_name, message_text, timestamp, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, message_id, author_id, author_name,
                  message_text, timestamp, media_type))
            return result.rowcount == 1

    def get(self, *, chat_id: int, message_id: int) -> MessageRecord | None:
        with self._connect() as con:
            row = con.execute("""
                SELECT chat_id, message_id, author_id, author_name,
                       message_text, timestamp, media_type
                FROM monitored_messages_v3
                WHERE chat_id = ? AND message_id = ?
            """, (chat_id, message_id)).fetchone()
        return MessageRecord(*row) if row else None

    def update(self, *, chat_id: int, message_id: int,
               message_text: str, media_type: str) -> bool:
        with self._connect() as con:
            result = con.execute("""
                UPDATE monitored_messages_v3
                SET message_text = ?, media_type = ?
                WHERE chat_id = ? AND message_id = ?
            """, (message_text, media_type, chat_id, message_id))
            return result.rowcount == 1

    def delete(self, *, chat_id: int, message_id: int) -> bool:
        with self._connect() as con:
            result = con.execute("""
                DELETE FROM monitored_messages_v3
                WHERE chat_id = ? AND message_id = ?
            """, (chat_id, message_id))
            return result.rowcount == 1

    def delete_old_messages(self, cutoff_timestamp: str) -> int:
        with self._connect() as con:
            result = con.execute(
                "DELETE FROM monitored_messages_v3 WHERE timestamp < ?",
                (cutoff_timestamp,),
            )
            return result.rowcount


def parse_chat_ids(value: str) -> set[int]:
    """Empty list means deny-all; reject malformed values instead of broadening access."""
    value = value.strip().strip('"').strip("'")
    if not value:
        return set()
    ids: set[int] = set()
    for item in value.split(","):
        token = item.strip()
        if not token or token in ("0", "+0", "-0"):
            raise ValueError("Each chat ID must be a nonzero integer")
        try:
            num = int(token, 10)
        except ValueError as error:
            raise ValueError(f"Invalid chat ID: {token!r}") from error
        if not num:
            raise ValueError("Chat ID cannot be zero")
        ids.add(num)
    return ids
