"""Dashboard + JSON-API."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import scheduler
from ..analysis import kpis, pacing, snapshot
from ..approval import approve, pending_ads, publish_approved, reject
from ..creative import media_root
from ..clock import today
from ..config import settings
from ..db import get_sessionmaker, init_engine
from ..executor import set_client_paused
from ..fmt import action_nl, euro, num, pct, status_nl
from ..models import OBJECTIVE_LABELS, OBJECTIVES, PLATFORM_LABELS, PLATFORMS, Action, Ad, AgentRun, Client
from ..clock import days_in_month
from ..planner import preview_plan
from ..runner import approval_url, run_client

log = logging.getLogger(__name__)
security = HTTPBasic(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    if os.environ.get("ADAGENT_DISABLE_SCHEDULER") != "1":
        scheduler.start()
    if not os.environ.get("ADAGENT_PASSWORD"):
        log.warning("ADAGENT_PASSWORD is niet gezet: dashboard is zonder login bereikbaar!")
    yield
    scheduler.shutdown()


app = FastAPI(title="JUST.LEAD Advertentie Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(
    PLATFORM_LABELS=PLATFORM_LABELS, OBJECTIVE_LABELS=OBJECTIVE_LABELS,
    PLATFORMS=PLATFORMS, OBJECTIVES=OBJECTIVES, settings=settings,
)


templates.env.filters.update(euro=euro, num=num, pct=pct, nl=status_nl, actie=action_nl)


def auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    user = os.environ.get("ADAGENT_USERNAME", "admin")
    pw = os.environ.get("ADAGENT_PASSWORD")
    if not pw:
        return
    ok = credentials is not None and secrets.compare_digest(credentials.username, user) and \
        secrets.compare_digest(credentials.password, pw)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})


def db():
    s = get_sessionmaker()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _client(s: Session, cid: int) -> Client:
    c = s.get(Client, cid)
    if not c:
        raise HTTPException(404, "Klant niet gevonden")
    return c


def _lines(v: str) -> list[str]:
    return [x.strip() for x in v.replace(";", "\n").splitlines() if x.strip()]


def _num(v: str) -> float | None:
    """Leest ook Nederlandse notatie: 3.000 · 3.000,50 · 35,5 · 35.5"""
    v = (v or "").strip().replace("€", "").replace(" ", "").replace("\u00a0", "")
    if not v:
        return None
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", v):
        v = v.replace(".", "")
    v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


# ---------------- HTML ----------------
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def index(request: Request, s: Session = Depends(db)):
    d = today()
    rows = []
    for c in s.scalars(select(Client).order_by(Client.name)):
        ids = [x.id for x in c.campaigns]
        k7 = kpis(s, campaign_ids=ids, start=d - dt.timedelta(days=7), end=d - dt.timedelta(days=1))
        p = pacing(s, c, d)
        rows.append({"c": c, "k7": k7, "p": p, "last": c.runs[0] if c.runs else None, "pending": len(pending_ads(c)),
                     "active": sum(1 for x in c.campaigns if x.status == "active")})
    return templates.TemplateResponse(request, "index.html", {"rows": rows})


@app.get("/clients/new", response_class=HTMLResponse, dependencies=[Depends(auth)])
def new_client(request: Request):
    return templates.TemplateResponse(request, "client_form.html", {"c": None})


def _apply_form(c: Client, form: dict) -> None:
    c.name = form["name"].strip()
    c.website = form.get("website", "").strip()
    c.description = form.get("description", "").strip()
    c.audience = form.get("audience", "").strip()
    c.locations = [x.upper() for x in _lines(form.get("locations", "NL").replace(",", "\n"))] or ["NL"]
    c.language = form.get("language", "nl").strip() or "nl"
    c.monthly_budget = _num(form.get("monthly_budget", "0")) or 0.0
    c.themes = _lines(form.get("themes", ""))
    c.objectives = [o for o in OBJECTIVES if form.get(f"obj_{o}")]
    c.platforms = [p for p in PLATFORMS if form.get(f"pf_{p}")]
    c.target_cpm = _num(form.get("target_cpm", ""))
    c.target_cpc = _num(form.get("target_cpc", ""))
    c.target_cpl = _num(form.get("target_cpl", ""))
    c.target_roas = _num(form.get("target_roas", ""))
    c.autopilot = bool(form.get("autopilot"))
    c.creative_approval = bool(form.get("creative_approval"))
    try:
        c.approval_timeout_hours = max(0, int(form.get("approval_timeout_hours") or 0))
    except ValueError:
        c.approval_timeout_hours = 0
    for f in ("brand_color", "accent_color"):
        v = (form.get(f) or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            setattr(c, f, v.lower())
    for f in ("meta_ad_account_id", "meta_page_id", "meta_pixel_id", "google_customer_id",
              "linkedin_ad_account_id", "linkedin_org_id", "linkedin_lead_form_id"):
        setattr(c, f, form.get(f, "").strip())


ALLOWED_IMG = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


async def _save_uploads(c: Client, form) -> list[str]:
    """Logo en foto's opslaan in de mediamap van de klant. Geeft foutmeldingen terug."""
    from PIL import Image

    errs = []
    folder = media_root() / "clients" / str(c.id)
    folder.mkdir(parents=True, exist_ok=True)

    async def store(up, name: str) -> str | None:
        if not getattr(up, "filename", ""):
            return None
        ext = ALLOWED_IMG.get(up.content_type or "")
        data = await up.read()
        if not ext:
            errs.append(f"{up.filename}: alleen PNG, JPG of WebP")
            return None
        if len(data) > 15 * 1024 * 1024:
            errs.append(f"{up.filename}: maximaal 15 MB")
            return None
        path = folder / f"{name}{ext}"
        path.write_bytes(data)
        try:
            Image.open(path).verify()
        except Exception:  # noqa: BLE001
            path.unlink(missing_ok=True)
            errs.append(f"{up.filename}: geen geldige afbeelding")
            return None
        return str(path)

    logo = form.get("logo")
    if logo is not None and not isinstance(logo, str):
        saved = await store(logo, "logo")
        if saved:
            c.logo_path = saved
    photos = list(c.photos or [])
    for up in form.getlist("photos"):
        if isinstance(up, str):
            continue
        saved = await store(up, f"foto-{secrets.token_hex(4)}")
        if saved:
            photos.append(saved)
    remove = set(form.getlist("remove_photo"))
    c.photos = [ph for ph in photos if Path(ph).name not in remove][:12]
    if form.get("remove_logo"):
        c.logo_path = ""
    return errs


def _validate(c: Client) -> list[str]:
    errs = []
    if not c.name:
        errs.append("Naam is verplicht")
    if c.monthly_budget <= 0:
        errs.append("Maandbudget moet groter dan 0 zijn")
    if not c.themes:
        errs.append("Vul minimaal één thema in")
    if not c.objectives:
        errs.append("Kies minimaal één doel")
    if not c.platforms:
        errs.append("Kies minimaal één platform")
    if not c.website.startswith(("http://", "https://")):
        errs.append("Website moet beginnen met https://")
    return errs


@app.post("/clients", dependencies=[Depends(auth)])
async def create_client(request: Request, s: Session = Depends(db)):
    raw = await request.form()
    form = {k: v for k, v in raw.items() if isinstance(v, str)}
    c = Client()
    _apply_form(c, form)
    errs = _validate(c)
    if errs:
        return templates.TemplateResponse(request, "client_form.html", {"c": c, "errors": errs, "form": form},
                                          status_code=400)
    s.add(c)
    s.flush()
    await _save_uploads(c, raw)
    if form.get("launch_now"):
        run_client(s, c, kind="setup")
    target = f"/akkoord/{c.approval_token}" if pending_ads(c) else f"/clients/{c.id}"
    return RedirectResponse(target, status_code=303)


@app.get("/clients/{cid}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def client_detail(request: Request, cid: int, s: Session = Depends(db)):
    c = _client(s, cid)
    d = today()
    snap = snapshot(s, c, d)
    actions = s.scalars(select(Action).where(Action.client_id == cid).order_by(Action.id.desc()).limit(80)).all()
    return templates.TemplateResponse(request, "client.html", {
        "c": c, "snap": snap, "actions": actions, "runs": c.runs[:15],
        "last": c.runs[0] if c.runs else None, "pending": len(pending_ads(c)), "approval_url": approval_url(c),
    })


@app.get("/clients/{cid}/edit", response_class=HTMLResponse, dependencies=[Depends(auth)])
def edit_client(request: Request, cid: int, s: Session = Depends(db)):
    return templates.TemplateResponse(request, "client_form.html", {"c": _client(s, cid)})


@app.post("/clients/{cid}/edit", dependencies=[Depends(auth)])
async def update_client(request: Request, cid: int, s: Session = Depends(db)):
    c = _client(s, cid)
    raw = await request.form()
    form = {k: v for k, v in raw.items() if isinstance(v, str)}
    _apply_form(c, form)
    errs = _validate(c) + await _save_uploads(c, raw)
    if errs:
        s.rollback()
        return templates.TemplateResponse(request, "client_form.html", {"c": c, "errors": errs, "form": form},
                                          status_code=400)
    return RedirectResponse(f"/clients/{cid}", status_code=303)


@app.post("/clients/{cid}/run", dependencies=[Depends(auth)])
def run_now(cid: int, s: Session = Depends(db)):
    c = _client(s, cid)
    run_client(s, c, kind="manual")
    return RedirectResponse(f"/clients/{cid}#rapport", status_code=303)


@app.post("/clients/{cid}/pause", dependencies=[Depends(auth)])
def pause(cid: int, s: Session = Depends(db)):
    set_client_paused(s, _client(s, cid), True)
    return RedirectResponse(f"/clients/{cid}", status_code=303)


@app.post("/clients/{cid}/resume", dependencies=[Depends(auth)])
def resume(cid: int, s: Session = Depends(db)):
    set_client_paused(s, _client(s, cid), False)
    return RedirectResponse(f"/clients/{cid}", status_code=303)


@app.post("/clients/{cid}/autopilot", dependencies=[Depends(auth)])
def toggle_autopilot(cid: int, s: Session = Depends(db)):
    c = _client(s, cid)
    c.autopilot = not c.autopilot
    return RedirectResponse(f"/clients/{cid}", status_code=303)


@app.get("/clients/{cid}/media/{name}", dependencies=[Depends(auth)])
def client_media(cid: int, name: str, s: Session = Depends(db)):
    _client(s, cid)
    folder = (media_root() / "clients" / str(cid)).resolve()
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/runs/{rid}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def run_detail(request: Request, rid: int, s: Session = Depends(db)):
    r = s.get(AgentRun, rid)
    if not r:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "run.html", {"r": r, "c": r.client})


# ---------------- Akkoord (klant, via geheime link zonder login) ----------------
def _by_token(s: Session, token: str) -> Client:
    c = s.scalar(select(Client).where(Client.approval_token == token)) if len(token) >= 16 else None
    if not c:
        raise HTTPException(404, "Deze akkoordlink is niet (meer) geldig")
    return c


def _own_ad(c: Client, ad_id: int, s: Session) -> Ad:
    ad = s.get(Ad, ad_id)
    if not ad or ad.campaign.client_id != c.id:
        raise HTTPException(404)
    return ad


@app.get("/akkoord/{token}", response_class=HTMLResponse)
def approval_page(request: Request, token: str, s: Session = Depends(db)):
    c = _by_token(s, token)
    waiting = pending_ads(c)
    done = sorted([a for x in c.campaigns for a in x.ads if a.approval in ("approved", "rejected")],
                  key=lambda a: a.approved_at or a.created_at, reverse=True)[:12]
    return templates.TemplateResponse(request, "approval.html", {
        "c": c, "waiting": waiting, "done": done, "token": token,
        "msg": request.query_params.get("m", ""),
    })


@app.get("/akkoord/{token}/beeld/{ad_id}/{fmt}")
def approval_image(token: str, ad_id: int, fmt: str, s: Session = Depends(db)):
    c = _by_token(s, token)
    ad = _own_ad(c, ad_id, s)
    path = (ad.assets or {}).get(fmt)
    root = media_root().resolve()
    if not path or not Path(path).resolve().is_relative_to(root) or not Path(path).exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/akkoord/{token}/ad/{ad_id}")
async def approval_action(request: Request, token: str, ad_id: int, s: Session = Depends(db)):
    c = _by_token(s, token)
    ad = _own_ad(c, ad_id, s)
    form = await request.form()
    by = str(form.get("by", "")).strip()[:120]
    if form.get("action") == "reject":
        reject(s, c, ad, str(form.get("feedback", "")), by=by)
        msg = "afgewezen"
    else:
        approve(s, c, ad, by=by)
        publish_approved(s, c, today())
        msg = "goedgekeurd"
    return RedirectResponse(f"/akkoord/{token}?m={msg}#lijst", status_code=303)


@app.post("/akkoord/{token}/alles")
async def approval_all(request: Request, token: str, s: Session = Depends(db)):
    c = _by_token(s, token)
    form = await request.form()
    by = str(form.get("by", "")).strip()[:120]
    for ad in pending_ads(c):
        approve(s, c, ad, by=by)
    publish_approved(s, c, today())
    return RedirectResponse(f"/akkoord/{token}?m=alles", status_code=303)


# ---------------- JSON-API ----------------
class ClientIn(BaseModel):
    name: str
    website: str
    monthly_budget: float = Field(gt=0)
    themes: list[str] = Field(min_length=1)
    objectives: list[str] = Field(default_factory=lambda: list(OBJECTIVES))
    platforms: list[str] = Field(default_factory=lambda: list(PLATFORMS))
    description: str = ""
    audience: str = ""
    locations: list[str] = Field(default_factory=lambda: ["NL"])
    language: str = "nl"
    target_cpm: float | None = None
    target_cpc: float | None = None
    target_cpl: float | None = None
    target_roas: float | None = None
    autopilot: bool = True
    launch_now: bool = True
    creative_approval: bool = True
    approval_timeout_hours: int = 0
    brand_color: str = "#0a0a0b"
    accent_color: str = "#ff5a1f"
    meta_ad_account_id: str = ""
    meta_page_id: str = ""
    meta_pixel_id: str = ""
    google_customer_id: str = ""
    linkedin_ad_account_id: str = ""
    linkedin_org_id: str = ""
    linkedin_lead_form_id: str = ""


@app.get("/api/clients", dependencies=[Depends(auth)])
def api_clients(s: Session = Depends(db)):
    d = today()
    return [{"id": c.id, "name": c.name, "status": c.status, "autopilot": c.autopilot,
             "monthly_budget": c.monthly_budget, "pacing": pacing(s, c, d).to_dict()}
            for c in s.scalars(select(Client).order_by(Client.id))]


@app.post("/api/clients", dependencies=[Depends(auth)], status_code=201)
def api_create_client(body: ClientIn, s: Session = Depends(db)):
    data = body.model_dump()
    launch = data.pop("launch_now")
    bad = [o for o in data["objectives"] if o not in OBJECTIVES] + [p for p in data["platforms"] if p not in PLATFORMS]
    if bad:
        raise HTTPException(422, f"Onbekende waarden: {bad}")
    c = Client(**data)
    s.add(c)
    s.flush()
    run = run_client(s, c, kind="setup") if launch else None
    return {"id": c.id, "run_id": run.id if run else None, "report": run.report if run else None,
            "approval_url": approval_url(c), "pending_ads": len(pending_ads(c))}


@app.get("/api/clients/{cid}/approvals", dependencies=[Depends(auth)])
def api_approvals(cid: int, s: Session = Depends(db)):
    c = _client(s, cid)
    return {"approval_url": approval_url(c), "pending": [{
        "ad_id": a.id, "campaign": a.campaign.name, "platform": a.campaign.platform, "objective": a.campaign.objective,
        "headlines": a.headlines, "descriptions": a.descriptions, "primary_text": a.primary_text, "visual": a.visual,
        "images": {f: f"{approval_url(c)}/beeld/{a.id}/{f}" for f in (a.assets or {})},
    } for a in pending_ads(c)]}


class Decision(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    feedback: str = ""
    by: str = ""


@app.post("/api/ads/{ad_id}/decision", dependencies=[Depends(auth)])
def api_decision(ad_id: int, body: Decision, s: Session = Depends(db)):
    ad = s.get(Ad, ad_id)
    if not ad:
        raise HTTPException(404)
    c = ad.campaign.client
    if body.action == "reject":
        new = reject(s, c, ad, body.feedback, by=body.by)
        return {"status": ad.status, "replacement_ad_id": new.id if new else None}
    approve(s, c, ad, by=body.by)
    return {"status": ad.status, "published": publish_approved(s, c, today())}


class PreviewIn(BaseModel):
    monthly_budget: float = 0
    themes: list[str] = Field(default_factory=list)
    objectives: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    description: str = ""
    audience: str = ""
    meta_pixel_id: str = ""


@app.post("/api/plan-preview", dependencies=[Depends(auth)])
def api_plan_preview(body: PreviewIn):
    c = Client(
        name="preview", monthly_budget=body.monthly_budget, themes=body.themes,
        objectives=[o for o in body.objectives if o in OBJECTIVES],
        platforms=[p for p in body.platforms if p in PLATFORMS],
        description=body.description, audience=body.audience, meta_pixel_id=body.meta_pixel_id,
    )
    if body.monthly_budget <= 0:
        return {"daily_total": 0, "campaigns": [], "notes": []}
    return preview_plan(c, body.monthly_budget / days_in_month(today()))


@app.get("/api/clients/{cid}/snapshot", dependencies=[Depends(auth)])
def api_snapshot(cid: int, s: Session = Depends(db)):
    return snapshot(s, _client(s, cid), today())


@app.post("/api/clients/{cid}/run", dependencies=[Depends(auth)])
def api_run(cid: int, s: Session = Depends(db)):
    run = run_client(s, _client(s, cid), kind="manual")
    return {"run_id": run.id, "status": run.status, "mode": run.mode, "report": run.report,
            "actions": [{"type": a.type, "status": a.status, "message": a.message, "reason": a.reason}
                        for a in run.actions]}


@app.get("/health")
def health():
    return {"ok": True, "mode": settings.mode, "ai": settings.ai_enabled}
