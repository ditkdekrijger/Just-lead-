"""CLI: python -m adagent <commando>

  serve                      Start dashboard + dagelijkse planner
  run-daily                  Eenmalig alle klanten analyseren en optimaliseren (voor cron)
  simulate --days N          Simuleer N dagen (sandbox) om de agent te zien werken
                             (--approve-all: wachtende advertenties direct goedkeuren)
  demo                       Maak een voorbeeldklant aan en simuleer 21 dagen
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging

from .clock import today
from .config import settings
from .db import get_sessionmaker, init_engine
from .models import Client
from .runner import run_all, run_client


def _simulate(days: int, client_id: int | None, start: dt.date | None, approve_all: bool = False) -> None:
    if settings.live:
        raise SystemExit("Simuleren kan alleen met ADAGENT_MODE=simulate")
    Session = get_sessionmaker()
    start = start or today()
    with Session() as s:
        ids = [client_id] if client_id else [c.id for c in s.query(Client).all()]
    for i in range(days + 1):
        day = start + dt.timedelta(days=i)
        for cid in ids:
            with Session() as s:
                c = s.get(Client, cid)
                run = run_client(s, c, day)
                if approve_all:
                    # Sandbox: de klant keurt alles direct goed (in het echt via de akkoordpagina)
                    from .approval import approve, pending_ads, publish_approved

                    waiting = pending_ads(c)
                    for ad in waiting:
                        approve(s, c, ad, by="Demo (gesimuleerd akkoord)")
                    if waiting:
                        publish_approved(s, c, day)
                        print(f"{day} · {c.name}: {len(waiting)} advertenties goedgekeurd (gesimuleerd)")
                s.commit()
                n = len([a for a in run.actions if a.status == "applied"])
                print(f"{day} · {c.name}: {run.kind}/{run.mode} · {run.status} · {n} acties")
    with Session() as s:
        for cid in ids:
            c = s.get(Client, cid)
            print("\n" + "=" * 70 + "\n" + (c.runs[0].report if c.runs else ""))


def _demo() -> None:
    Session = get_sessionmaker()
    with Session() as s:
        c = Client(
            name="Demo Installatiebedrijf",
            website="https://www.voorbeeld-installatie.nl",
            description="Installateur van warmtepompen en zonnepanelen voor particulieren in Midden-Nederland.",
            audience="Huiseigenaren 35-65 jaar die hun huis willen verduurzamen",
            locations=["NL"],
            monthly_budget=3000,
            themes=["Warmtepomp", "Zonnepanelen"],
            objectives=["reach", "clicks", "leads", "sales"],
            platforms=["meta", "google", "linkedin"],
            target_cpl=35.0,
        )
        s.add(c)
        s.commit()
        cid = c.id
    _simulate(21, cid, today() - dt.timedelta(days=21), approve_all=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="adagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("run-daily")
    sm = sub.add_parser("simulate")
    sm.add_argument("--days", type=int, default=14)
    sm.add_argument("--client-id", type=int)
    sm.add_argument("--start", type=dt.date.fromisoformat)
    sm.add_argument("--approve-all", action="store_true", help="Wachtende advertenties direct goedkeuren")
    sub.add_parser("demo")
    a = p.parse_args()

    init_engine()
    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("adagent.web.app:app", host=a.host, port=a.port)
    elif a.cmd == "run-daily":
        ids = run_all(get_sessionmaker())
        print(f"{len(ids)} klanten verwerkt")
    elif a.cmd == "simulate":
        _simulate(a.days, a.client_id, a.start, a.approve_all)
    elif a.cmd == "demo":
        _demo()


if __name__ == "__main__":
    main()
