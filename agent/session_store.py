"""Wave 12 — 프로세스 밖(SQLite 파일)에 두는 세션 저장소.

서버가 재시작돼도(새 프로세스, 새 인스턴스) 같은 session.db 파일을 다시 열므로
대화 이력이 유지된다. in-memory dict였다면 재시작 즉시 소실됐을 상태다.
"""
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .contracts import ConversationContext

DB_PATH = Path(__file__).parent.parent / "session.db"


class SqliteSessionStore:
    def __init__(self, db_path: Path | None = None):
        db_path = db_path if db_path is not None else Path(os.getenv("SESSION_DB_PATH", str(DB_PATH)))
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._turn_locks = {}
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " thread_id TEXT, role TEXT, content TEXT, created TEXT)"
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS conversation_context (thread_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS message_metadata (message_rowid INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self.conn.commit()

    def turn_lock(self, thread_id: str):
        """같은 프로세스의 동일 대화 POST/SSE를 순서대로 처리한다."""
        return self._turn_locks.setdefault(thread_id, asyncio.Lock())

    def load_context(self, thread_id: str) -> ConversationContext:
        row = self.conn.execute("SELECT payload FROM conversation_context WHERE thread_id=?", (thread_id,)).fetchone()
        context = ConversationContext.model_validate_json(row[0]) if row else ConversationContext(thread_id=thread_id)
        if context.thread_id != thread_id:
            raise ValueError("저장된 문맥의 대화 ID 불일치")
        return context

    def history(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT m.rowid,m.role,m.content,m.created,a.payload FROM messages m "
            "LEFT JOIN message_metadata a ON a.message_rowid=m.rowid WHERE m.thread_id=? ORDER BY m.rowid",
            (thread_id,),
        ).fetchall()
        result = []
        for rowid, role, content, created, payload in rows:
            metadata = json.loads(payload) if payload is not None else None
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("저장된 응답 상태 형식 오류")
            result.append({"id": rowid, "role": role, "content": content, "created_at": created, "metadata": metadata})
        return result

    def save_turn(self, thread_id: str, query: str, reply: str, context: ConversationContext | None, metadata: dict):
        """메시지 쌍·상태·문맥을 하나의 트랜잭션으로 저장한다. 실패 시 모두 롤백한다."""
        if context is not None and context.thread_id != thread_id:
            raise ValueError("문맥의 대화 ID 불일치")
        payload = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        context_json = context.model_dump_json() if context else None
        created = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO messages VALUES (?,?,?,?)", (thread_id, "user", query, created))
            cursor = self.conn.execute("INSERT INTO messages VALUES (?,?,?,?)", (thread_id, "assistant", reply, created))
            self.conn.execute("INSERT INTO message_metadata VALUES (?,?)", (cursor.lastrowid, payload))
            if context_json is not None:
                self.conn.execute("INSERT INTO conversation_context VALUES (?,?) "
                                  "ON CONFLICT(thread_id) DO UPDATE SET payload=excluded.payload", (thread_id, context_json))

    def load(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE thread_id=? ORDER BY rowid",
            (thread_id,),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def append(self, thread_id: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?)",
            (thread_id, role, content, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
