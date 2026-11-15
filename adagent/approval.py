"""Akkoordstap voor advertentiemateriaal.

Flow: de agent maakt advertenties + beelden → status 'pending' (klaar voor akkoord)
→ klant of accountmanager keurt goed of wijst af met feedback
→ goedgekeurd: direct publiceren op het platform (nieuwe campagnes worden dan pas gelanceerd)
→ afgewezen: de agent maakt een nieuwe versie op basis van de feedback, weer ter akkoord.

Budgetwijzigingen en pauzes vallen niet onder de akkoordstap; die regelt de autopilot.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from .copywriting import CTA_BY_OBJECTIVE, make_copy
from .creative import create_assets
from .models import Action, Ad, Campaign, Client
from .platforms import PlatformError, get_connector

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def pending_ads(client: Client) -> list[Ad]:
    """Wachtende advertenties; beeldadvertenties (Meta, LinkedIn) eerst."""
    order = {"meta": 0, "linkedin": 1, "google": 2}
    ads = [a for c in client.campaigns for a in c.ads if a.status == "pending" and a.approval == "pending"]
    return sorted(ads, key=lambda a: (order.get(a.campaign.platform, 9), a.campaign_id, a.id))


def prepare_ad(client: Client, campaign: Campaign, ad: Ad) -> None:
    """Beeldmateriaal maken en de juiste akkoordstatus zetten (nog niet publiceren)."""
    create_assets(client, campaign, ad)
    if client.creative_approval:
        ad.status, ad.approval = "pending", "pending"
    else:
        ad.status, ad.approval = "active", "auto"


def approve(session: Session, client: Client, ad: Ad, by: str = "") -> None:
    if ad.approval != "pending":
        return
    ad.approval, ad.approved_by, ad.approved_at = "approved", by[:120], _now()
    ad.status = "active"
    session.add(Action(client_id=client.id, campaign_id=ad.campaign_id, ad_id=ad.id, type="approve_ad",
                       reason=f"Goedgekeurd{' door ' + by if by else ''}", status="applied",
                       message=(ad.headlines or [""])[0]))


def reject(session: Session, client: Client, ad: Ad, feedback: str, by: str = "") -> Ad | None:
    """Afwijzen en direct een nieuwe versie maken die de feedback verwerkt."""
    if ad.approval != "pending":
        return None
    ad.approval, ad.status, ad.feedback = "rejected", "rejected", feedback.strip()[:2000]
    ad.approved_by, ad.approved_at = by[:120], _now()
    session.add(Action(client_id=client.id, campaign_id=ad.campaign_id, ad_id=ad.id, type="reject_ad",
                       reason=feedback.strip()[:500] or "Afgewezen zonder toelichting", status="applied",
                       message=(ad.headlines or [""])[0]))
    camp = ad.campaign
    avoid = [a.headlines[0] for a in camp.ads if a.headlines]
    learn = f"De klant wees deze versie af. Feedback: {feedback.strip() or 'geen toelichting'}. Kies een duidelijk andere invalshoek."
    copy = make_copy(client, ad.theme or camp.theme, camp.objective, camp.platform,
                     variant=len(camp.ads) + 1, learnings=learn, avoid=avoid)
    new = Ad(theme=ad.theme or camp.theme, headlines=copy.headlines, descriptions=copy.descriptions,
             primary_text=copy.primary_text, cta=CTA_BY_OBJECTIVE[camp.objective], landing_url=ad.landing_url,
             generation=ad.generation + 1, feedback=feedback.strip()[:2000])
    camp.ads.append(new)
    session.flush()
    prepare_ad(client, camp, new)
    session.add(Action(client_id=client.id, campaign_id=camp.id, ad_id=new.id, type="create_ad", status="applied",
                       reason="Nieuwe versie na feedback", message=f"Advertentie {new.id} klaargezet voor akkoord"))
    return new


def publish_approved(session: Session, client: Client, today: dt.date) -> list[str]:
    """Publiceert alles wat is goedgekeurd: nieuwe campagnes lanceren, nieuwe advertenties toevoegen."""
    msgs: list[str] = []
    conns: dict = {}
    for camp in client.campaigns:
        ready = [a for a in camp.ads if a.status == "active" and not a.external_id]
        if not ready:
            continue
        try:
            conn = conns.setdefault(camp.platform, get_connector(client, camp.platform))
            if camp.status == "pending":
                conn.create_campaign(camp, ready, active=client.autopilot)
                camp.status = "active" if client.autopilot else "paused"
                camp.paused_by = "" if client.autopilot else "user"
                camp.launched_on = today
                msg = f"Campagne gelanceerd met {len(ready)} goedgekeurde advertentie(s): {camp.name}"
                session.add(Action(client_id=client.id, campaign_id=camp.id, type="launch_campaign",
                                   params={"daily_budget": camp.daily_budget, "ads": len(ready)},
                                   reason="Na akkoord op het advertentiemateriaal", status="applied", message=msg))
            else:
                for ad in ready:
                    conn.create_ad(camp, ad)
                msg = f"{len(ready)} goedgekeurde advertentie(s) gepubliceerd in {camp.name}"
                session.add(Action(client_id=client.id, campaign_id=camp.id, type="publish_ad",
                                   reason="Na akkoord", status="applied", message=msg))
            msgs.append(msg)
        except (PlatformError, Exception) as e:  # noqa: BLE001
            log.exception("Publiceren mislukt")
            if camp.status == "pending":
                camp.status, camp.error = "error", str(e)[:2000]
            session.add(Action(client_id=client.id, campaign_id=camp.id, type="publish_ad", status="failed",
                               message=str(e)[:2000]))
    if msgs and client.status == "new":
        client.status = "active"
    session.flush()
    return msgs


def auto_approve_expired(session: Session, client: Client, now: dt.datetime | None = None) -> int:
    """Optioneel: na X uur zonder reactie automatisch goedkeuren (instelling per klant, 0 = uit)."""
    hours = client.approval_timeout_hours or 0
    if hours <= 0:
        return 0
    now = now or _now()
    n = 0
    for ad in pending_ads(client):
        created = ad.created_at if ad.created_at.tzinfo else ad.created_at.replace(tzinfo=dt.timezone.utc)
        if now - created >= dt.timedelta(hours=hours):
            approve(session, client, ad, by=f"automatisch na {hours} uur")
            n += 1
    return n
