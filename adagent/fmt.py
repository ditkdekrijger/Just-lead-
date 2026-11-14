"""Nederlandse getal- en labelnotatie."""

from __future__ import annotations


def _nl(s: str) -> str:
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def euro(v, decimals: int = 2) -> str:
    return "–" if v is None else "€" + _nl(f"{v:,.{decimals}f}")


def num(v, decimals: int = 0) -> str:
    return "–" if v is None else _nl(f"{v:,.{decimals}f}")


def pct(v) -> str:
    return "–" if v is None else _nl(f"{v * 100:.2f}") + "%"


STATUS_NL = {
    "active": "actief", "paused": "gepauzeerd", "error": "fout", "new": "nieuw", "done": "klaar",
    "running": "bezig", "applied": "uitgevoerd", "rejected": "geweigerd", "failed": "mislukt",
    "proposed": "voorstel", "setup": "opstart", "daily": "dagelijks", "manual": "handmatig",
}
ACTION_NL = {
    "set_budget": "Budget aangepast", "pause_campaign": "Campagne gepauzeerd", "resume_campaign": "Campagne hervat",
    "pause_ad": "Advertentie gepauzeerd", "create_ad": "Nieuwe advertentie", "launch_campaign": "Campagne gelanceerd",
    "strategy": "Strategie",
}


def status_nl(v: str) -> str:
    return STATUS_NL.get(v, v)


def action_nl(v: str) -> str:
    return ACTION_NL.get(v, v)
