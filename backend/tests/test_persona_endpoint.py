"""GET/PATCH /api/persona — singleton CRUD."""
from backend.models.db import Setting


def _seed_first_run(test_db):
    test_db.add(Setting(key="dashboard_api_key", value=""))
    test_db.commit()


def test_persona_get_returns_singleton(api_client, test_db):
    """GET returns the seeded persona with all 8 nodes."""
    _seed_first_run(test_db)
    from backend.seed import seed_persona
    seed_persona(test_db)

    resp = api_client.get("/api/persona")
    assert resp.status_code == 200
    data = resp.json()
    for k in ("contact", "work_auth", "demographics", "compensation",
              "preferences", "resume_content", "qa_bank"):
        assert k in data, f"missing node: {k}"


def test_persona_patch_merges_into_node(api_client, test_db):
    """PATCH updates one node without touching others."""
    _seed_first_run(test_db)
    from backend.seed import seed_persona
    seed_persona(test_db)

    resp = api_client.patch(
        "/api/persona",
        json={"contact": {"name": "Jordan Blake", "email": "jordan@example.com"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["contact"] == {"name": "Jordan Blake", "email": "jordan@example.com"}
    # Other nodes untouched
    assert data["work_auth"] == {}


def test_persona_patch_rejects_unknown_node(api_client, test_db):
    """PATCH with an unknown key gets 400, doesn't silently mutate."""
    _seed_first_run(test_db)
    from backend.seed import seed_persona
    seed_persona(test_db)

    resp = api_client.patch("/api/persona", json={"random_node": {"x": 1}})
    assert resp.status_code == 400


def test_persona_patch_replaces_node_atomically(api_client, test_db):
    """PATCH replaces the WHOLE node content; doesn't deep-merge inside it.
    Caller is responsible for sending the complete node value."""
    _seed_first_run(test_db)
    from backend.seed import seed_persona
    from backend.models.db import Persona
    seed_persona(test_db)

    p = test_db.query(Persona).filter(Persona.id == 1).first()
    p.contact = {"name": "Alice", "phone": "+1"}
    test_db.commit()

    resp = api_client.patch("/api/persona", json={"contact": {"name": "Bob"}})
    assert resp.status_code == 200
    assert resp.json()["contact"] == {"name": "Bob"}  # phone gone


def test_profile_page_save_keeps_certifications_and_other_keys_it_does_not_edit(api_client, test_db):
    """The Profile page sends back the résumé sections it knows; keys it has no editor for (certifications,
    anything an import adds) survive the save. Sections it does send are replaced, not merged."""
    _seed_first_run(test_db)
    from backend.seed import seed_persona
    seed_persona(test_db)
    certs = [{"title": "Google Network Security Specialization", "description": "Google"},
             {"title": "Building Production-Ready AI Agents", "description": "AWS"}]
    first = {"summary": "old", "skills": {"Languages": ["Python"]}, "experience": [{"title": "SWE"}], "certifications": certs,
             "future_section": {"x": 1}}
    assert api_client.patch("/api/persona", json={"resume_content": first}).status_code == 200
    # a Profile-page-shaped save: the known sections only
    page = {"header": {"name": "V R", "contact_items": []}, "summary": "new", "experience": [], "skills": {},
            "education": [], "projects": [], "publications": []}
    resp = api_client.patch("/api/persona", json={"resume_content": page})
    assert resp.status_code == 200
    rc = resp.json()["resume_content"]
    assert rc["certifications"] == certs and rc["future_section"] == {"x": 1}
    assert rc["summary"] == "new" and rc["experience"] == [] and rc["skills"] == {}       # sent sections replace
    assert api_client.get("/api/persona").json()["resume_content"]["certifications"] == certs
    # the certifications editor's own save changes them
    rc2 = api_client.patch("/api/persona", json={"resume_content": {**page, "certifications": certs[:1]}}).json()["resume_content"]
    assert rc2["certifications"] == certs[:1]
