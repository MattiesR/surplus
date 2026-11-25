"""Engine/session factory and migrations. SQLite in WAL mode (plan §3.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

ALEMBIC_DIR = Path(__file__).parent / "alembic"


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def migrate(url: str) -> None:
    """Bring the database to the latest schema."""
    command.upgrade(alembic_config(url), "head")


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
