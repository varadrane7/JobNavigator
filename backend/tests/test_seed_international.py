"""scripts/seed_international.py: a linked profile set up as a relocation lane, idempotently."""
import json

from backend.accounts import auth as A
from backend.accounts.context import unscoped
from backend.accounts.settings import resolve_for, section_mode
from backend.models.db import Account, Company, Persona, Search
from backend.scripts import seed_international as S


def test_seeds_a_linked_relocation_profile_once(test_db):
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    plan = S.run(test_db, "Abroad")
    test_db.commit()
    prof = test_db.query(Account).filter(Account.name == "Abroad").one()
    assert prof.role == "member" and prof.parent_account_id == owner.id
    assert any("create linked profile" in line for line in plan)

    with unscoped():
        assert json.loads(resolve_for(test_db, prof.id, "curate_countries")[0]) == ["DE", "NL", "IE", "AE"]
        assert resolve_for(test_db, prof.id, "curate_count")[0] == "20"
        assert section_mode(test_db, prof.id, "linkedin") == "inherit"
        searches = {s.name: s for s in test_db.query(Search).filter(Search.account_id == prof.id).all()}
        assert searches["UAE · backend (LinkedIn, Indeed, Bayt)"].sources == ["linkedin", "indeed", "bayt"]
        assert searches["DE · Arbeitnow"].search_mode == "arbeitnow"
        companies = test_db.query(Company).filter(Company.account_id == prof.id).all()
        assert len(companies) == len(S.COMPANIES) and all(c.active for c in companies)
        persona = test_db.query(Persona).filter(Persona.account_id == prof.id).one()
        assert "Blue Card" in persona.preferences["visa_route"]
        # the owner's own lane is untouched
        assert test_db.query(Search).filter(Search.account_id == owner.id, Search.name.like("DE ·%")).count() == 0

    accounts = A.load_accounts(test_db)
    assert {a["id"] for a in A.linked_family(accounts, owner.id)} == {owner.id, prof.id}

    n_search = len(searches)
    again = S.run(test_db, "Abroad")
    test_db.commit()
    with unscoped():
        assert test_db.query(Search).filter(Search.account_id == prof.id).count() == n_search
        assert test_db.query(Company).filter(Company.account_id == prof.id).count() == len(S.COMPANIES)
    assert not any("create" in line or "search " in line or "companies" in line for line in again)
