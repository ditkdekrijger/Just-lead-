"""Database-engine en sessies."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    url = url or settings.db_url
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    _engine = create_engine(url, future=True, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    from . import models  # noqa: F401  (registreert tabellen)

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    return _engine


def _add_missing_columns(engine) -> None:
    """Lichte migratie: nieuwe kolommen toevoegen aan bestaande tabellen (met hun standaardwaarde)."""
    import json

    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if col.default is not None and callable(col.default.arg):
                    try:
                        default = col.default.arg(None)
                    except TypeError:
                        default = None
                    if isinstance(default, str) and len(default) > 20:
                        default = None  # bv. tokens: per rij invullen hieronder
                if isinstance(default, bool):
                    ddl += f" DEFAULT {int(default)}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                elif isinstance(default, (list, dict)):
                    ddl += " DEFAULT '" + json.dumps(default) + "'"
                conn.execute(text(ddl))
        if insp.has_table("clients"):
            import secrets

            for (cid,) in conn.execute(text("SELECT id FROM clients WHERE approval_token IS NULL OR approval_token = ''")):
                conn.execute(text("UPDATE clients SET approval_token = :t WHERE id = :i"),
                             {"t": secrets.token_urlsafe(24), "i": cid})


def get_sessionmaker() -> sessionmaker:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
