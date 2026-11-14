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
