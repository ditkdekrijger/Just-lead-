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
