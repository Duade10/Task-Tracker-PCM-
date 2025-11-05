from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _utcnow() -> str:
    return datetime.utcnow().strftime(ISO_FORMAT)


@dataclass
class Task:
    id: int
    title: str
    description: str
    developer_id: str
    project_manager_id: str
    created_at: str
    completed_at: Optional[str]
    developer_checked: bool
    project_manager_checked: bool
    channel_id: str
    message_ts: Optional[str]


class TaskRepository:
    def __init__(self, db_path: str | Path = "tasks.db") -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    developer_id TEXT NOT NULL,
                    project_manager_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    developer_checked INTEGER NOT NULL DEFAULT 0,
                    project_manager_checked INTEGER NOT NULL DEFAULT 0,
                    channel_id TEXT NOT NULL,
                    message_ts TEXT
                )
                """
            )

            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "title" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT")
                conn.execute(
                    "UPDATE tasks SET title = description WHERE title IS NULL OR title = ''"
                )

    def create_task(
        self,
        title: str,
        description: str,
        developer_id: str,
        project_manager_id: str,
        channel_id: str,
    ) -> Task:
        created_at = _utcnow()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    title,
                    description,
                    developer_id,
                    project_manager_id,
                    created_at,
                    channel_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    description,
                    developer_id,
                    project_manager_id,
                    created_at,
                    channel_id,
                ),
            )
            task_id = cursor.lastrowid
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise RuntimeError("Failed to retrieve newly created task")
        return self._row_to_task(row)

    def update_message_reference(self, task_id: int, channel_id: str, message_ts: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET channel_id = ?, message_ts = ? WHERE id = ?",
                (channel_id, message_ts, task_id),
            )

    def get_task(self, task_id: int) -> Task:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task {task_id} not found")
        return self._row_to_task(row)

    def update_checkmarks(
        self,
        task_id: int,
        developer_checked: bool,
        project_manager_checked: bool,
    ) -> Task:
        existing = self.get_task(task_id)
        if developer_checked and project_manager_checked:
            completed_at = existing.completed_at or _utcnow()
        else:
            completed_at = None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                   SET developer_checked = ?,
                       project_manager_checked = ?,
                       completed_at = ?
                 WHERE id = ?
                """,
                (
                    int(developer_checked),
                    int(project_manager_checked),
                    completed_at,
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def list_tasks(
        self,
        status: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> list[Task]:
        conditions, params = self._build_filters(status, start, end)
        query = "SELECT * FROM tasks"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        limit_params: list[object] = []
        if limit is not None:
            query += " LIMIT ?"
            limit_params.append(limit)
            if offset is not None:
                query += " OFFSET ?"
                limit_params.append(offset)
        elif offset is not None:
            query += " LIMIT -1 OFFSET ?"
            limit_params.append(offset)
        with self._connect() as conn:
            rows = conn.execute(query, (*params, *limit_params)).fetchall()
        return [self._row_to_task(row) for row in rows]

    def count_tasks(
        self,
        status: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> int:
        conditions, params = self._build_filters(status, start, end)
        query = "SELECT COUNT(*) FROM tasks"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def delete_task(self, task_id: int) -> None:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            if cursor.rowcount == 0:
                raise KeyError(f"Task {task_id} not found")

    def _build_filters(
        self,
        status: Optional[str],
        start: Optional[str],
        end: Optional[str],
    ) -> tuple[list[str], list[object]]:
        conditions: list[str] = []
        params: list[object] = []
        if status == "completed":
            conditions.append("developer_checked = 1 AND project_manager_checked = 1")
        elif status == "pending":
            conditions.append("NOT (developer_checked = 1 AND project_manager_checked = 1)")
        if start:
            conditions.append("created_at >= ?")
            params.append(start)
        if end:
            conditions.append("created_at < ?")
            params.append(end)
        return conditions, params

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"] if "title" in row.keys() else row["description"],
            description=row["description"],
            developer_id=row["developer_id"],
            project_manager_id=row["project_manager_id"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            developer_checked=bool(row["developer_checked"]),
            project_manager_checked=bool(row["project_manager_checked"]),
            channel_id=row["channel_id"],
            message_ts=row["message_ts"],
        )

