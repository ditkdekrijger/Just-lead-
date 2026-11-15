from fastapi.testclient import TestClient


def test_dashboard_flow(session, monkeypatch, tmp_path):
    from adagent import db
    from adagent.web import app as webapp

    monkeypatch.setattr(webapp, "init_engine", lambda: None)
    with TestClient(webapp.app) as tc:
        assert tc.get("/").status_code == 200
        assert "Nieuwe klant" in tc.get("/clients/new").text
        r = tc.post("/clients", data={"name": "Bakkerij Jansen", "website": "https://bakkerij.nl",
                                      "monthly_budget": "1500", "themes": "Taarten\nBrood",
                                      "obj_leads": "1", "obj_sales": "1", "pf_meta": "1", "pf_google": "1",
                                      "autopilot": "1", "launch_now": "1"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        url = r.headers["location"]
        page = tc.get(url).text
        assert "Bakkerij Jansen" in page and "Opstart" in page and "Google Ads" in page
        assert tc.post(url + "/run", follow_redirects=False).status_code == 303
        assert tc.post(url + "/pause", follow_redirects=False).status_code == 303
        assert "Hervatten" in tc.get(url).text
        bad = tc.post("/clients", data={"name": "", "website": "x", "monthly_budget": "0", "themes": ""})
        assert bad.status_code == 400 and "Maandbudget" in bad.text

        r = tc.post("/api/clients", json={"name": "API klant", "website": "https://api.nl", "monthly_budget": 2000,
                                          "themes": ["CRM software"], "description": "B2B SaaS"})
        assert r.status_code == 201 and r.json()["report"]
        cid = r.json()["id"]
        snap = tc.get(f"/api/clients/{cid}/snapshot").json()
        assert snap["campaigns"]
        assert tc.get("/health").json()["ok"]


def test_basic_auth(session, monkeypatch):
    from adagent.web import app as webapp

    monkeypatch.setattr(webapp, "init_engine", lambda: None)
    monkeypatch.setenv("ADAGENT_PASSWORD", "geheim")
    with TestClient(webapp.app) as tc:
        assert tc.get("/").status_code == 401
        assert tc.get("/", auth=("admin", "geheim")).status_code == 200


def test_plan_preview_and_intake_form(session, monkeypatch):
    from adagent.web import app as webapp

    monkeypatch.setattr(webapp, "init_engine", lambda: None)
    with TestClient(webapp.app) as tc:
        page = tc.get("/clients/new").text
        assert 'id="plan' not in page and "Wat de agent gaat bouwen" in page and 'name="themes"' in page
        r = tc.post("/api/plan-preview", json={"monthly_budget": 3000, "themes": ["Warmtepomp", "Zonnepanelen"],
                                               "objectives": ["reach", "clicks", "leads", "sales"],
                                               "platforms": ["meta", "google", "linkedin"],
                                               "description": "Installateur voor particulieren"}).json()
        assert len(r["campaigns"]) == 13
        assert any("LinkedIn valt af" in n["title"] for n in r["notes"])
        small = tc.post("/api/plan-preview", json={"monthly_budget": 800, "themes": ["A", "B"],
                                                   "objectives": ["reach", "clicks", "leads", "sales"],
                                                   "platforms": ["meta", "google", "linkedin"]}).json()
        assert any("vallen af" in n["title"] for n in small["notes"])
        assert tc.post("/api/plan-preview", json={"monthly_budget": 0}).json()["campaigns"] == []
        # Nederlandse notatie in het formulier
        r = tc.post("/clients", data={"name": "Fietsenwinkel", "website": "https://fiets.nl", "monthly_budget": "2.500",
                                      "themes": "E-bikes\nOnderhoud", "obj_leads": "1", "pf_google": "1",
                                      "locations": "NL,BE", "target_cpl": "22,50", "autopilot": "1", "launch_now": "1"},
                    follow_redirects=False)
        assert r.status_code == 303
        from adagent.models import Client

        c = session.query(Client).filter_by(name="Fietsenwinkel").one()
        assert c.monthly_budget == 2500 and c.target_cpl == 22.5 and c.locations == ["NL", "BE"]
        assert c.campaigns
        edit = tc.get(f"/clients/{c.id}/edit").text
        assert "Klant bewerken" in edit and "E-bikes" in edit and "Opslaan" in edit


def test_approval_flow_via_web(session, monkeypatch, tmp_path):
    import io

    from PIL import Image

    from adagent.models import Client
    from adagent.web import app as webapp

    monkeypatch.setattr(webapp, "init_engine", lambda: None)
    monkeypatch.setenv("ADAGENT_PASSWORD", "geheim")
    buf = io.BytesIO()
    Image.new("RGB", (900, 700), (30, 90, 60)).save(buf, "JPEG")
    with TestClient(webapp.app) as tc:
        auth = ("admin", "geheim")
        r = tc.post("/clients", auth=auth, data={
            "name": "Hoveniersbedrijf Groen", "website": "https://groen.nl", "monthly_budget": "2.000",
            "themes": "Tuinontwerp", "obj_leads": "1", "pf_meta": "1", "pf_google": "1", "autopilot": "1",
            "creative_approval": "1", "brand_color": "#1e5a3c", "accent_color": "#f2c14e", "launch_now": "1"},
            files=[("photos", ("tuin.jpg", buf.getvalue(), "image/jpeg")),
                   ("logo", ("x.txt", b"geen beeld", "text/plain"))], follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/akkoord/")
        link = r.headers["location"]
        c = session.query(Client).filter_by(name="Hoveniersbedrijf Groen").one()
        session.refresh(c)
        assert c.brand_color == "#1e5a3c" and len(c.photos) == 1 and c.logo_path == ""
        assert all(x.status == "pending" for x in c.campaigns)

        # Klant opent de link zonder login
        page = tc.get(link)
        assert page.status_code == 200 and "wacht" in page.text and "Goedkeuren" in page.text
        meta_ad = next(a for x in c.campaigns if x.platform == "meta" for a in x.ads)
        img = tc.get(f"{link}/beeld/{meta_ad.id}/1x1")
        assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
        assert tc.get(f"{link}/beeld/{meta_ad.id}/../../etc").status_code == 404
        assert tc.get("/akkoord/ongeldig-token-123456789").status_code == 404
        assert tc.get("/").status_code == 401  # dashboard blijft afgeschermd

        # Afwijzen met feedback → nieuwe versie
        r = tc.post(f"{link}/ad/{meta_ad.id}", data={"action": "reject", "feedback": "Liever een foto van een tuin", "by": "Klant"},
                    follow_redirects=False)
        assert r.status_code == 303
        session.expire_all()
        assert session.get(type(meta_ad), meta_ad.id).status == "rejected"

        # Alles goedkeuren → campagnes live
        r = tc.post(f"{link}/alles", data={"by": "Klant"}, follow_redirects=False)
        assert r.status_code == 303
        session.expire_all()
        c = session.query(Client).filter_by(name="Hoveniersbedrijf Groen").one()
        assert all(x.status == "active" and x.external_id for x in c.campaigns)
        assert "Niets te beoordelen" in tc.get(link).text
        assert tc.get(f"/clients/{c.id}", auth=auth).status_code == 200
        api = tc.get(f"/api/clients/{c.id}/approvals", auth=auth).json()
        assert api["pending"] == [] and api["approval_url"].endswith(c.approval_token)
