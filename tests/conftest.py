import datetime as dt
import os

import pytest

os.environ["ADAGENT_MODE"] = "simulate"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
os.environ["ADAGENT_DISABLE_SCHEDULER"] = "1"
os.environ.pop("ADAGENT_PASSWORD", None)

from adagent import config  # noqa: E402

config.reload_settings()


@pytest.fixture()
def session(tmp_path, monkeypatch):
    from adagent import db

    monkeypatch.setattr(config.settings, "media_dir", str(tmp_path / "media"))
    db.init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    s = db.get_sessionmaker()()
    yield s
    s.close()


@pytest.fixture()
def make_client(session):
    from adagent.models import Client

    def _make(**kw):
        data = dict(
            name="Testklant", website="https://example.nl", monthly_budget=3000,
            themes=["Warmtepomp", "Zonnepanelen"], objectives=["reach", "clicks", "leads", "sales"],
            platforms=["meta", "google", "linkedin"], target_cpl=35.0, creative_approval=False,
        )
        data.update(kw)
        c = Client(**data)
        session.add(c)
        session.flush()
        return c

    return _make


START = dt.date(2026, 3, 2)
