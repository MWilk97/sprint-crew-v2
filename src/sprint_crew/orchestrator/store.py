"""Shared sqlite key→JSON-payload persistence for orchestrator stores."""

from __future__ import annotations

import json
import sqlite3
import typing
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, cast

from pydantic import BaseModel


@lru_cache(maxsize=32)
def _cached_store(store_cls: type, db_path: Path):
    """One store instance per (class, database file).

    The `*_store()` factories are called per operation, and constructing a store runs
    `_init_db` — so a single `load()` opened two connections: one to re-create a table that
    already existed, one to read. Keyed on the path so a test that repoints
    SPRINT_SESSION_DB gets its own instance. A store holds no connection, only a path, so
    sharing one across threads is safe.
    """
    return store_cls(db_path)


class SqliteJsonStore:
    """One-table upsert/load store; subclasses define the table layout.

    ``create_sql`` must create the table with ``key_column`` as PRIMARY KEY and
    a ``payload`` TEXT column (plus any extra columns the subclass writes).
    """

    table: ClassVar[str]
    key_column: ClassVar[str]
    create_sql: ClassVar[str]

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False because connections are opened per-operation and may be
        # created off the event loop's thread. busy_timeout is per-connection so it has to
        # be set every time; journal_mode is persisted in the database header, so it is set
        # once in _init_db rather than costing a write on every read.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        # WAL lets SSE readers coexist with a writing run instead of hitting "database is
        # locked". It survives in the file header, so later connections inherit it.
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(self.create_sql)

    def _save_row(self, columns: dict[str, str]) -> None:
        names = list(columns)
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != self.key_column)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"INSERT INTO {self.table} ({', '.join(names)}) VALUES ({placeholders}) "
                f"ON CONFLICT({self.key_column}) DO UPDATE SET {updates}",
                tuple(columns[name] for name in names),
            )

    def _load_payload(self, key: str) -> str | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                f"SELECT payload FROM {self.table} WHERE {self.key_column} = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["payload"])

    def _delete(self, key: str) -> bool:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                f"DELETE FROM {self.table} WHERE {self.key_column} = ?",
                (key,),
            )
        return cur.rowcount > 0

    def _list_payloads(self) -> list[str]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(f"SELECT payload FROM {self.table}").fetchall()
        return [str(row["payload"]) for row in rows]

    def _clear_all(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(f"DELETE FROM {self.table}")


class TypedJsonStore[T: BaseModel](SqliteJsonStore):
    """A ``SqliteJsonStore`` whose payload is one Pydantic model.

    The session, backlog-run and console-session stores each hand-rolled the identical
    save/load/list_all trio — nine lines apiece differing only in the model class. The
    key column is named after the model's own id field in all three, so the base can
    derive the key rather than have each subclass restate it.

    ``model`` is checked against the ``T`` in the subclass's base at class-creation time.
    Without that the parameter is decorative: ``class S(TypedJsonStore[ConsoleSession])``
    with ``model = BacklogRun`` type-checks, and only fails when a payload is validated in
    production.
    """

    model: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        declared = _declared_parameter(cls)
        model = cls.__dict__.get("model")
        if declared is None or model is None:
            # An intermediate subclass may legitimately declare neither; the concrete one
            # that adds `model` gets checked.
            return
        if model is not declared:
            raise TypeError(
                f"{cls.__name__} is a TypedJsonStore[{declared.__name__}] but sets "
                f"model = {model.__name__}; they must be the same model."
            )

    def _extra_columns(self, item: T) -> dict[str, str]:
        """Columns beyond key + payload. Override to mirror a model field into a column.

        Replaces a ``tracks_updated_at`` flag that made the base reach for
        ``item.updated_at`` on a plain ``BaseModel`` — untypeable, and silently wrong for a
        model without the field.
        """
        return {}

    def save(self, item: T) -> None:
        columns = {
            self.key_column: str(getattr(item, self.key_column)),
            "payload": json.dumps(item.model_dump(mode="json")),
            **self._extra_columns(item),
        }
        self._save_row(columns)

    def load(self, key: str) -> T | None:
        payload = self._load_payload(key)
        if payload is None:
            return None
        return cast(T, self.model.model_validate(json.loads(payload)))

    def list_all(self) -> list[T]:
        return [cast(T, self.model.model_validate(json.loads(p))) for p in self._list_payloads()]


def _declared_parameter(cls: type) -> type[BaseModel] | None:
    """The ``T`` a subclass wrote in ``TypedJsonStore[T]``, if it wrote one."""
    for base in getattr(cls, "__orig_bases__", ()):
        if typing.get_origin(base) is not TypedJsonStore:
            continue
        args = typing.get_args(base)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None
