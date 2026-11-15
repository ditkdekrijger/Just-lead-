"""Advertentiemateriaal: beeldbrief (AI of template) en rendering naar afbeeldingen per formaat.

Elke advertentie krijgt een beeld in drie formaten:
- 1x1      1080×1080  Meta feed, LinkedIn (vierkant)
- 4x5      1080×1350  Meta feed (staand, meeste ruimte in de tijdlijn)
- 191x100  1200×628   LinkedIn single image, linkvoorbeelden

Drie stijlen: 'statement' (merkkleur met grote kop), 'checklist' (kop + drie voordelen)
en 'photo' (eigen foto van de klant met donkere overloop en tekst).
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

from . import llm
from .config import settings
from .copywriting import AdCopy, fit
from .models import OBJECTIVE_LABELS, Ad, Campaign, Client

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
FORMATS = {"1x1": (1080, 1080), "4x5": (1080, 1350), "191x100": (1200, 628)}
#: Welke formaten een platform gebruikt (Google Search toont geen beeld)
PLATFORM_FORMATS = {"meta": ["1x1", "4x5"], "linkedin": ["191x100", "1x1"], "google": []}
TEMPLATES = ("statement", "checklist", "photo")
CTA_LABEL = {"reach": "Ontdek meer", "clicks": "Bekijk het aanbod", "leads": "Vraag advies aan", "sales": "Bestel nu"}


class VisualBrief(BaseModel):
    headline: str = Field(description="Kop op het beeld, max 38 tekens, sterk en concreet")
    subline: str = Field(description="Ondertitel op het beeld, max 70 tekens")
    badge: str = Field(description="Kort label linksboven, max 22 tekens, bv. 'Gratis advies' of 'Nieuw'")
    bullets: list[str] = Field(description="Drie korte voordelen, elk max 32 tekens")
    cta: str = Field(description="Knoptekst op het beeld, max 22 tekens")
    template: str = Field(description="statement, checklist of photo")


# ---------------- Brief ----------------
def template_brief(client: Client, ad: Ad, campaign: Campaign) -> VisualBrief:
    cta_l = CTA_LABEL[campaign.objective].lower()
    heads = [h for h in (ad.headlines or []) if "|" not in h and len(h) >= 14
             and cta_l not in h.lower() and "vrijblijvend" not in h.lower() and not h.lower().startswith("bestel")]
    head = heads[0] if heads else ((ad.headlines or [campaign.theme])[0].split("|")[0].strip() or campaign.theme)
    rng = random.Random(f"{campaign.id}-{ad.generation}-{ad.id}")
    has_photo = bool(client.photos)
    order = (["photo"] if has_photo else []) + ["statement", "checklist"]
    tpl = order[(ad.generation - 1 + (ad.id or 0)) % len(order)] if ad.id else order[0]
    # Ondertitel: de informatiefste zin uit de hoofdtekst (geen vraag, geen herhaling van de knop)
    sentences = [x.strip(" 👉") for x in re.split(r"(?<=[.!?])\s+", ad.primary_text or "") if x.strip()]
    cta = CTA_LABEL[campaign.objective].lower()
    good = [x for x in sentences if not x.endswith("?") and cta not in x.lower()
            and "vrijblijvend advies" not in x.lower() and 12 <= len(x) <= 70]
    desc = good[0] if good else max([*(ad.descriptions or []), *sentences, ""], key=len)
    bullets = ["Persoonlijk advies", "Heldere afspraken", "Snel geschakeld"]
    rng.shuffle(bullets)
    return VisualBrief(
        headline=fit(head, 38), subline=fit(desc, 70),
        badge={"leads": "Vrijblijvend", "sales": "Direct bestellen", "reach": client.name, "clicks": "Bekijk online"}[campaign.objective][:22],
        bullets=bullets, cta=CTA_LABEL[campaign.objective], template=tpl,
    )


VISUAL_SYSTEM = (
    "Je bent art director bij JUST.LEAD (powermarketing). Je maakt de tekst voor advertentiebeelden die in "
    "een tijdlijn in één oogopslag overtuigen: één scherpe belofte, concreet, geen jargon, geen verzonnen "
    "prijzen, kortingen of claims. Schrijf in de taal van de klant."
)


def ai_brief(client: Client, ad: Ad, campaign: Campaign) -> VisualBrief:
    prompt = f"""Maak de beeldtekst voor deze advertentie.

Klant: {client.name} — {client.description or '-'}
Doelgroep: {client.audience or '-'}
Thema: {ad.theme or campaign.theme}
Doel: {OBJECTIVE_LABELS[campaign.objective]} · platform: {campaign.platform}
Advertentiekop: {(ad.headlines or [''])[0]}
Advertentietekst: {ad.primary_text}
{('Feedback van de klant op een eerdere versie: ' + ad.feedback) if ad.feedback else ''}
Beschikbare stijlen: statement (kop op merkkleur), checklist (kop + 3 voordelen){', photo (eigen foto van de klant)' if client.photos else ''}.
Kies de stijl die het best past en wissel af met andere varianten."""
    brief = llm.structured(VISUAL_SYSTEM, prompt, VisualBrief, max_tokens=2000)
    if brief.template not in TEMPLATES or (brief.template == "photo" and not client.photos):
        brief.template = "statement"
    return brief


def make_brief(client: Client, ad: Ad, campaign: Campaign) -> VisualBrief:
    if settings.ai_enabled:
        try:
            b = ai_brief(client, ad, campaign)
        except llm.LLMError as e:
            log.warning("AI-beeldbrief mislukt, template gebruikt: %s", e)
            b = template_brief(client, ad, campaign)
    else:
        b = template_brief(client, ad, campaign)
    b.headline, b.subline, b.badge, b.cta = fit(b.headline, 38), fit(b.subline, 70), fit(b.badge, 22), fit(b.cta, 22)
    b.bullets = [fit(x, 32) for x in b.bullets[:3] if x.strip()] or ["Persoonlijk advies"]
    return b


def brief_from_copy(copy: AdCopy, visual_headline: str = "", visual_subline: str = "") -> dict:
    """Voor varianten die de agent zelf schrijft: beeldtekst meegeven als die er is."""
    return {k: v for k, v in {"headline": visual_headline, "subline": visual_subline}.items() if v}


# ---------------- Rendering ----------------
def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    name = "Archivo-ExtraBold.ttf" if bold else "Archivo-Medium.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default(size)


def _hex(c: str, fallback: str) -> tuple[int, int, int]:
    c = (c or fallback).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return _hex(fallback, "#0a0a0b")


def _lum(rgb: tuple[int, int, int]) -> float:
    def ch(v: int) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ink(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    return (10, 10, 11) if _lum(bg) > 0.4 else (255, 255, 255)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines


def _fit_headline(draw, text, max_w, max_h, start, min_size=36, max_lines=4):
    size = start
    while size > min_size:
        f = _font(size)
        lines = _wrap(draw, text, f, max_w, max_lines)
        h = len(lines) * int(size * 1.02)
        if h <= max_h and all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f, lines, size
        size -= 4
    f = _font(min_size)
    return f, _wrap(draw, text, f, max_w, max_lines), min_size


def _pill(draw, xy, text, font, fill, ink, pad=(28, 16)):
    x, y = xy
    w = draw.textlength(text, font=font)
    asc, desc = font.getmetrics()
    h = asc + desc
    draw.rounded_rectangle([x, y, x + w + pad[0] * 2, y + h + pad[1] * 2], radius=999, fill=fill)
    draw.text((x + pad[0], y + pad[1]), text, font=font, fill=ink)
    return w + pad[0] * 2, h + pad[1] * 2


def _logo(img: Image.Image, client: Client, right: int, top: int, height: int, max_w: int, ink) -> None:
    """Logo (of naam) rechtsboven, rechts uitgelijnd."""
    if client.logo_path and Path(client.logo_path).exists():
        try:
            logo = Image.open(client.logo_path).convert("RGBA")
            logo.thumbnail((max_w, height))
            img.paste(logo, (right - logo.width, top), logo)
            return
        except OSError:
            pass
    d = ImageDraw.Draw(img)
    size = int(height * 0.75)
    f = _font(size)
    while d.textlength(client.name, font=f) > max_w and size > 14:
        size -= 2
        f = _font(size)
    d.text((right - d.textlength(client.name, font=f), top), client.name, font=f, fill=ink)


def render(client: Client, brief: VisualBrief, fmt: str, photo: str | None = None) -> Image.Image:
    W, H = FORMATS[fmt]
    brand = _hex(client.brand_color, "#0a0a0b")
    accent = _hex(client.accent_color, "#ff5a1f")
    wide = W / H > 1.5
    m = int(min(W, H) * (0.075 if not wide else 0.07))
    tpl = brief.template if brief.template in TEMPLATES else "statement"
    if tpl == "photo" and not (photo and Path(photo).exists()):
        tpl = "statement"

    if tpl == "checklist":
        bg = (243, 243, 243) if _lum(brand) < 0.5 else (10, 10, 11)
    else:
        bg = brand
    img = Image.new("RGB", (W, H), bg)
    ink = _ink(bg)

    if tpl == "photo":
        ph = ImageOps.fit(Image.open(photo).convert("RGB"), (W, H), method=Image.LANCZOS)
        img.paste(ph)
        grad = Image.new("L", (1, H))
        for y in range(H):
            t = max(0.0, (y / H - 0.25) / 0.75)
            grad.putpixel((0, y), int(235 * t ** 1.3))
        shade = Image.new("RGB", (W, H), (10, 10, 11))
        img.paste(shade, (0, 0), grad.resize((W, H)))
        ink = (255, 255, 255)

    d = ImageDraw.Draw(img)
    accent_ink = _ink(accent)

    # Badge linksboven
    bf = _font(int(min(W, H) * 0.028), bold=False)
    _pill(d, (m, m), brief.badge, bf, accent, accent_ink, pad=(int(bf.size * 0.9), int(bf.size * 0.5)))

    # Logo/naam rechtsboven
    _logo(img, client, W - m, m, int(min(W, H) * 0.05), int(W * 0.4), ink)

    # Tekstblok
    cta_f = _font(int(min(W, H) * 0.034))
    sub_f = _font(int(min(W, H) * (0.036 if not wide else 0.042)), bold=False)
    col_w = int(W * (0.58 if wide else 1)) - (m if wide else 2 * m)
    asc, desc = cta_f.getmetrics()
    cta_h = asc + desc + int(cta_f.size * 1.1)

    if tpl == "checklist":
        top = m + int(min(W, H) * 0.14)
        hf, hl, hs = _fit_headline(d, brief.headline, col_w, int(H * (0.30 if not wide else 0.42)), int(min(W, H) * 0.11))
        y = top
        for line in hl:
            d.text((m, y), line, font=hf, fill=ink)
            y += int(hs * 1.02)
        y += int(hs * 0.45)
        bfnt = _font(int(min(W, H) * 0.04), bold=False)
        r = int(bfnt.size * 0.62)
        bx = m if not wide else int(W * 0.58) + m // 2
        by = y if not wide else top
        bw = col_w if not wide else W - bx - m
        for b in brief.bullets[:3]:
            d.ellipse([bx, by + 4, bx + 2 * r, by + 4 + 2 * r], fill=accent)
            cx, cy = bx + r, by + 4 + r
            d.line([(cx - r * 0.45, cy), (cx - r * 0.1, cy + r * 0.38), (cx + r * 0.5, cy - r * 0.35)],
                   fill=accent_ink, width=max(3, r // 4))
            lines = _wrap(d, b, bfnt, bw - 2 * r - 20, 2)
            for i, ln in enumerate(lines):
                d.text((bx + 2 * r + 20, by + i * int(bfnt.size * 1.15)), ln, font=bfnt, fill=ink)
            by += int(bfnt.size * 1.15) * len(lines) + int(bfnt.size * 0.7)
        _pill(d, (m, H - m - cta_h), brief.cta, cta_f, accent, accent_ink,
              pad=(int(cta_f.size * 1.0), int(cta_f.size * 0.55)))
        return img

    # statement en photo: tekst onderin
    sub_lines = _wrap(d, brief.subline, sub_f, col_w, 2)
    sub_h = len(sub_lines) * int(sub_f.size * 1.3)
    avail = int(H * (0.45 if not wide else 0.5))
    hf, hl, hs = _fit_headline(d, brief.headline, col_w, avail, int(min(W, H) * (0.12 if not wide else 0.13)))
    head_h = len(hl) * int(hs * 1.02)
    y = H - m - cta_h - int(sub_f.size * 1.2) - sub_h - int(hs * 0.35) - head_h
    if tpl == "statement":
        _arch(d, W, H, m, y, wide, accent)
    for line in hl:
        d.text((m, y), line, font=hf, fill=ink)
        y += int(hs * 1.02)
    y += int(hs * 0.35)
    sub_ink = tuple(int(c * 0.82 + (bg[i] if tpl != "photo" else 10) * 0.18) for i, c in enumerate(ink))
    for ln in sub_lines:
        d.text((m, y), ln, font=sub_f, fill=sub_ink)
        y += int(sub_f.size * 1.3)
    _pill(d, (m, H - m - cta_h), brief.cta, cta_f, accent, accent_ink,
          pad=(int(cta_f.size * 1.0), int(cta_f.size * 0.55)))
    return img


def _arch(d: ImageDraw.ImageDraw, W: int, H: int, m: int, text_top: int, wide: bool, color) -> None:
    """Merkmotief (dubbele boog) in de lege ruimte rechtsboven, nooit door de tekst heen."""
    if wide:
        R = int(H * 0.36)
        x1 = W - m
        top, bottom = int(H * 0.2), H
    else:
        top = m + int(min(W, H) * 0.12)
        bottom = text_top - int(m * 0.6)
        R = min(int(W * 0.2), (bottom - top) // 2)
        x1 = W - m
        if R < W * 0.08:
            return
    cx, cy = x1 - R, top + R
    for k in (0, int(R * 0.2)):
        d.arc([cx - R + k, cy - R + k, cx + R - k, cy + R - k], start=180, end=360, fill=color, width=6)
        d.line([(cx - R + k, cy), (cx - R + k, bottom)], fill=color, width=6)
        d.line([(cx + R - k - 5, cy), (cx + R - k - 5, bottom)], fill=color, width=6)


def media_root() -> Path:
    p = Path(settings.media_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def create_assets(client: Client, campaign: Campaign, ad: Ad) -> None:
    """Brief maken (indien nodig) en alle formaten voor dit platform renderen."""
    formats = PLATFORM_FORMATS.get(campaign.platform, [])
    existing = ad.visual or {}
    if not existing.get("headline") or not existing.get("template"):
        brief = make_brief(client, ad, campaign)
        merged = {**brief.model_dump(), **{k: v for k, v in existing.items() if v}}
        ad.visual = merged
    if not formats:
        ad.assets = {}
        return
    brief = VisualBrief(**{**template_brief(client, ad, campaign).model_dump(), **ad.visual})
    photo = None
    if brief.template == "photo" and client.photos:
        photo = client.photos[(ad.id or 0) % len(client.photos)]
    folder = media_root() / "clients" / str(client.id)
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for fmt in formats:
        path = folder / f"ad{ad.id}-g{ad.generation}-{fmt}.png"
        render(client, brief, fmt, photo).save(path)
        out[fmt] = str(path)
    ad.assets = out
