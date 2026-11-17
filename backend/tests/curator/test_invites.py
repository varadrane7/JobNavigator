"""The daily invite cap (curator/invites.py): invited_on, the cap setting, carry-over, over-cap flag."""
import random
from datetime import date, timedelta

import pytest

from backend.accounts.settings import put_setting
from backend.curator import invites, leads
from backend.models.db import Contact
from backend.tz import local_today


def _lead(db, name, status="new", score=50, due=True, **kw):
    c = Contact(name=name, company="Co", linkedin_url=f"https://www.linkedin.com/in/{name.lower()}", status=status,
                score=score, next_action_at=local_today() if due else None, found_on=local_today(), **kw)
    db.add(c)
    db.commit()
    return c


# ── the setting ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [(None, 8), ("", 8), ("8", 8), ("5", 5), ("12", 12), ("13", 12), ("99", 12),
                                      ("0", 8), ("-3", 8), ("abc", 8), ("4.5", 8), (" 10 ", 10), (7, 7)])
def test_clamp(raw, want):
    assert invites.clamp_cap(raw) == want


def test_default_is_8_and_the_setting_is_read_per_account(test_db):
    assert invites.cap(test_db) == 8
    put_setting(test_db, "invites_daily_cap", "20")
    test_db.commit()
    assert invites.cap(test_db) == 12


# ── invited_on, set once through one helper ─────────────────────────────────

def test_mark_invited_sets_once_on_first_move_to_requested(test_db):
    c = _lead(test_db, "Ann")
    assert invites.mark_invited(c, "connected") is False and c.invited_on is None
    assert invites.mark_invited(c, "requested") is True and c.invited_on == local_today()
    c.invited_on = date(2026, 1, 2)
    assert invites.mark_invited(c, "requested") is False and c.invited_on == date(2026, 1, 2)


def test_patch_route_sets_invited_on_and_reports_the_cap(api_client, test_db):
    c = _lead(test_db, "Ann")
    r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "requested"})
    assert r.status_code == 200
    body = r.json()
    assert body["invited_on"] == local_today().isoformat()
    assert body["invites"] == {"sent_today": 1, "cap": 8, "remaining": 7, "over_cap_by": 0}
    # a later move does not change it
    test_db.expire_all()
    r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "connected"})
    assert r.json()["invited_on"] == local_today().isoformat()
    assert r.json()["invites"]["sent_today"] == 1


def test_patch_to_non_requested_status_leaves_invited_on_empty(api_client, test_db):
    c = _lead(test_db, "Ann")
    assert api_client.patch(f"/api/contacts/{c.id}", json={"status": "skip"}).json()["invited_on"] is None


def test_patch_accepts_beyond_the_cap_and_flags_it(api_client, test_db):
    put_setting(test_db, "invites_daily_cap", "2")
    test_db.commit()
    rows = [_lead(test_db, f"P{i}") for i in range(4)]
    last = None
    for c in rows:
        r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "requested"})
        assert r.status_code == 200 and r.json()["status"] == "requested"     # never refused
        last = r.json()["invites"]
    assert last == {"sent_today": 4, "cap": 2, "remaining": 0, "over_cap_by": 2}


def test_log_lead_sets_invited_on_to_the_contacted_date(test_db):
    lead, _ = leads.log_lead(test_db, {"name": "Asha Rao", "company": "Q", "status": "requested",
                                        "contacted_on": "2026-11-10"})
    assert lead.invited_on == date(2026, 11, 10)
    test_db.commit()
    lead2, _ = leads.log_lead(test_db, {"name": "Bo Li", "company": "Q", "status": "contacted"})
    assert lead2.invited_on == local_today()
    test_db.commit()
    # reported again later: unchanged
    again, created = leads.log_lead(test_db, {"name": "Asha Rao", "company": "Q", "status": "requested",
                                              "contacted_on": "2026-11-12"})
    assert not created and again.invited_on == date(2026, 11, 10)
    lead3, _ = leads.log_lead(test_db, {"name": "Cy Do", "company": "Q"})
    assert lead3.invited_on is None


def test_log_lead_backdated_invite_does_not_count_today(test_db):
    leads.log_lead(test_db, {"name": "Asha Rao", "company": "Q", "status": "requested", "contacted_on": "2026-11-10"})
    test_db.flush()
    assert invites.sent_today(test_db) == 0 and invites.sent_today(test_db, date(2026, 11, 10)) == 1


def test_post_contacts_route_reports_invites(api_client, test_db):
    r = api_client.post("/api/contacts", json={"name": "Dee", "company": "Z", "status": "requested"})
    assert r.status_code == 200
    assert r.json()["invites"]["sent_today"] == 1 and r.json()["invited_on"] == local_today().isoformat()


def test_mcp_update_lead_sets_invited_on_and_reports(test_db):
    from backend.connector import tools
    c = _lead(test_db, "Ann")
    out = tools.update_lead(test_db, {"lead_id": str(c.id), "status": "requested"})
    assert out["invites"]["sent_today"] == 1 and out["invites"]["cap"] == 8
    assert test_db.get(Contact, c.id).invited_on == local_today()
    again = tools.update_lead(test_db, {"lead_id": str(c.id), "status": "connected"})
    assert test_db.get(Contact, c.id).invited_on == local_today() and again["invites"]["sent_today"] == 1


def test_mcp_add_lead_sets_invited_on_and_flags_over_cap(test_db):
    from backend.connector import tools
    put_setting(test_db, "invites_daily_cap", "1")
    test_db.commit()
    tools.add_lead(test_db, {"name": "One", "company": "A", "status": "requested"})
    out = tools.add_lead(test_db, {"name": "Two", "company": "A", "status": "requested"})
    assert out["invites"]["over_cap_by"] == 1 and "over the cap by 1" in out["invites"]["note"]
    assert out["status"] == "requested"


# ── send today / carry-over ─────────────────────────────────────────────────

def test_only_cap_minus_sent_leads_are_send_today_the_rest_carry(api_client, test_db):
    put_setting(test_db, "invites_daily_cap", "3")
    test_db.commit()
    for i, s in enumerate([10, 90, 50, 70, 30]):
        _lead(test_db, f"N{i}", score=s)
    _lead(test_db, "Sent", status="requested", score=99, due=False, invited_on=local_today())
    inv = api_client.get("/api/contacts/invites").json()
    assert inv == {"sent_today": 1, "cap": 3, "remaining": 2, "over_cap_by": 0, "send_today": 2, "carried": 3,
                   "drafts_enabled": True}
    rows = api_client.get("/api/contacts", params={"due": True}).json()
    assert [r["name"] for r in rows] == ["N1", "N3"]            # best score first, the rest carried
    assert all(r["send_today"] for r in rows)
    # the carried leads are not dropped: still new, still due, visible under "new"
    allnew = api_client.get("/api/contacts", params={"status": "new"}).json()
    assert len(allnew) == 5
    assert {r["name"] for r in allnew if r["carried"]} == {"N0", "N2", "N4"}


def test_follow_ups_are_not_held_back_by_the_cap(api_client, test_db):
    put_setting(test_db, "invites_daily_cap", "1")
    test_db.commit()
    _lead(test_db, "New1", score=80)
    _lead(test_db, "New2", score=70)
    _lead(test_db, "Chase", status="requested", score=10, invited_on=local_today() - timedelta(days=5))
    names = [r["name"] for r in api_client.get("/api/contacts", params={"due": True}).json()]
    assert "Chase" in names and "New1" in names and "New2" not in names


def test_when_the_cap_is_used_up_no_new_lead_is_send_today(api_client, test_db):
    put_setting(test_db, "invites_daily_cap", "2")
    test_db.commit()
    for i in range(3):
        _lead(test_db, f"S{i}", status="requested", due=False, invited_on=local_today())
    _lead(test_db, "Waiting")
    inv = api_client.get("/api/contacts/invites").json()
    assert (inv["remaining"], inv["over_cap_by"], inv["send_today"], inv["carried"]) == (0, 1, 0, 1)
    assert api_client.get("/api/contacts", params={"due": True}).json() == []


def test_tomorrow_the_carried_lead_comes_up_first_again(test_db):
    put_setting(test_db, "invites_daily_cap", "1")
    test_db.commit()
    _lead(test_db, "A", score=60)
    _lead(test_db, "B", score=40)
    p = invites.plan(test_db)
    assert [c for c in p["send"]] == [test_db.query(Contact).filter_by(name="A").one().id]
    tomorrow = local_today() + timedelta(days=1)
    p2 = invites.plan(test_db, tomorrow)       # nobody was invited on `tomorrow`: the cap is fresh
    assert p2["sent_today"] == 0 and len(p2["send"]) == 1


def test_sent_today_uses_the_local_day(test_db, monkeypatch):
    _lead(test_db, "Old", status="requested", due=False, invited_on=local_today() - timedelta(days=1))
    assert invites.sent_today(test_db) == 0


def test_mcp_list_leads_and_today_show_the_cap(test_db):
    from backend.connector import tools
    put_setting(test_db, "invites_daily_cap", "2")
    test_db.commit()
    for i, s in enumerate([60, 80, 70]):
        _lead(test_db, f"L{i}", score=s)
    out = tools.list_leads(test_db, {"due": True})
    assert out["invites"]["cap"] == 2 and out["invites"]["carried_to_tomorrow"] == 1
    assert [r["name"] for r in out["leads"]] == ["L1", "L2"] and all(r["send_today"] for r in out["leads"])
    assert tools.today(test_db, {})["invites"]["sent_today"] == 0


# ── the nag ─────────────────────────────────────────────────────────────────

NAG_CFG = {"apply_t": 10, "req_t": 5, "calendar": {}, "url": "https://jn.example"}


def test_nag_stats_carry_the_invite_numbers_and_leave_carried_leads_out_of_due(test_db):
    from backend.curator.nag import stats
    put_setting(test_db, "invites_daily_cap", "1")
    test_db.commit()
    _lead(test_db, "A", score=60)
    _lead(test_db, "B", score=40)
    s = stats(local_today())
    assert (s["invited"], s["invite_cap"], s["due"]) == (0, 1, 1)


def test_nag_stops_asking_for_requests_at_the_cap():
    from backend.curator.nag import compose
    s = {"picks": 3, "apps": 2, "reqs": 8, "due": 1, "streak": 2, "invited": 8, "invite_cap": 8}
    text = compose(s, NAG_CFG, 14, date(2026, 11, 16), random.Random(1))
    assert "Invite cap reached: 8/8" in text
    assert "requests short" not in text


def test_nag_request_target_never_exceeds_the_cap():
    from backend.curator.nag import compose
    s = {"picks": 3, "apps": 2, "reqs": 1, "due": 1, "streak": 2, "invited": 1, "invite_cap": 3}
    cfg = {**NAG_CFG, "req_t": 5}
    for seed in range(12):
        text = compose(s, cfg, 18, date(2026, 11, 16), random.Random(seed))
        assert "/5 " not in text and "of 5" not in text
    assert "Invites today: 1/3, 2 left to send." in compose(s, cfg, 14, date(2026, 11, 16), random.Random(1))


def test_nag_is_unchanged_without_invite_numbers():
    from backend.curator.nag import compose
    s = {"picks": 5, "apps": 3, "reqs": 1, "due": 2, "streak": 3}
    assert "Invite" not in compose(s, NAG_CFG, 18, date(2026, 11, 16), random.Random(2))


# ── backfill, scoping, re-entry ─────────────────────────────────────────────

def test_backfill_touches_only_requested_leads_with_no_invited_on_and_runs_once(test_db):
    from sqlalchemy import text
    from backend.seed import invited_on_backfill_sql
    from backend.tz import day_start
    stamp = day_start(date(2026, 11, 9))
    rows = {
        "req": _lead(test_db, "Req", status="requested", due=False, last_action_at=stamp),
        "req_has": _lead(test_db, "ReqHas", status="requested", due=False, last_action_at=stamp, invited_on=date(2026, 1, 5)),
        "req_none": _lead(test_db, "ReqNone", status="requested", due=False),
        "connected": _lead(test_db, "Conn", status="connected", due=False, last_action_at=stamp),
        "new": _lead(test_db, "New", status="new", due=False, last_action_at=stamp),
    }
    sql = text(invited_on_backfill_sql("sqlite"))
    first = test_db.execute(sql).rowcount
    test_db.commit()
    assert first == 1
    test_db.expire_all()
    got = {k: test_db.get(Contact, c.id).invited_on for k, c in rows.items()}
    assert got["req"] is not None and got["req"].year == 2026 and got["req"].month == 11
    assert got["req_has"] == date(2026, 1, 5)           # an existing date is never replaced
    assert got["req_none"] is None and got["connected"] is None and got["new"] is None
    assert test_db.execute(sql).rowcount == 0           # the second run changes nothing
    assert "AT TIME ZONE" in invited_on_backfill_sql("postgresql")


def test_two_accounts_keep_separate_invite_counts_and_caps(test_db):
    from backend.accounts.context import use_account
    from backend.models.db import Account
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    member = Account(name="Partner", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    with use_account(owner.id):
        _lead(test_db, "O1", status="requested", due=False, invited_on=local_today())
        _lead(test_db, "O2", status="requested", due=False, invited_on=local_today())
    with use_account(member.id):
        _lead(test_db, "M1", status="requested", due=False, invited_on=local_today())
        put_setting(test_db, "invites_daily_cap", "3", account_id=member.id)
        test_db.commit()
        assert invites.state(test_db) == {"sent_today": 1, "cap": 3, "remaining": 2, "over_cap_by": 0}
    with use_account(owner.id):
        assert invites.state(test_db) == {"sent_today": 2, "cap": 8, "remaining": 6, "over_cap_by": 0}


def test_the_nag_counts_per_account_and_matches_the_chip(test_db):
    from backend.accounts.context import use_account
    from backend.curator.nag import stats
    from backend.models.db import Account
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    member = Account(name="Partner", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    with use_account(owner.id):
        _lead(test_db, "O1", status="requested", due=False, invited_on=local_today())
        owner_stats = stats(local_today())
        assert owner_stats["reqs"] == owner_stats["invited"] == invites.sent_today(test_db) == 1
    with use_account(member.id):
        assert stats(local_today())["reqs"] == 0


def test_a_moved_on_lead_keeps_its_first_invite_date_when_requested_again(test_db):
    c = _lead(test_db, "Ann")
    invites.mark_invited(c, "requested", date(2026, 11, 2))
    c.status = "connected"
    invites.mark_invited(c, "requested")                # re-entry: set once, never again
    assert c.invited_on == date(2026, 11, 2)


def test_due_list_is_filtered_before_the_limit_and_ordered_like_the_plan(api_client, test_db):
    put_setting(test_db, "invites_daily_cap", "2")
    test_db.commit()
    yesterday = local_today() - timedelta(days=1)
    for i in range(5):
        _lead(test_db, f"N{i}", score=10 * i)
    old = _lead(test_db, "Old", score=1)
    old.next_action_at = yesterday
    test_db.commit()
    chase = _lead(test_db, "Chase", status="requested", score=5, invited_on=yesterday)
    rows = api_client.get("/api/contacts", params={"due": True, "limit": 3}).json()
    names = [r["name"] for r in rows]
    # carried leads are removed in SQL, so the page of 3 is full and in the plan's order
    # audience (none here), then score, then age: the best scores are "send today", the old low score waits
    assert names == ["N4", "N3", "Chase"]
    plan_first = invites.plan(test_db)["send"]
    assert [c.name for c in map(lambda i: test_db.get(Contact, i), plan_first)] == ["N4", "N3"]


def test_a_lead_due_longer_than_the_ageing_days_comes_ahead_of_newer_ones(test_db):
    from backend.curator.invites import ageing_days
    put_setting(test_db, "invites_daily_cap", "2")
    test_db.commit()
    assert ageing_days(test_db) == 7
    _lead(test_db, "Fresh-alum-high", score=90, audience="alum")
    _lead(test_db, "Fresh-alum-mid", score=80, audience="alum")
    stale = _lead(test_db, "Stale-unknown-low", score=1)
    stale.next_action_at = local_today() - timedelta(days=8)
    older = _lead(test_db, "Six-days-low", score=1)
    older.next_action_at = local_today() - timedelta(days=6)
    test_db.commit()
    plan = invites.plan(test_db)
    names = lambda ids: [test_db.get(Contact, i).name for i in ids]
    assert names(plan["send"]) == ["Stale-unknown-low", "Fresh-alum-high"]       # aged first, then audience and score
    assert "Six-days-low" in names(plan["carried"])                              # not yet aged
    put_setting(test_db, "due_ageing_days", "5")
    test_db.commit()
    assert names(invites.plan(test_db)["send"])[:2] == ["Stale-unknown-low", "Six-days-low"]


def test_ageing_setting_falls_back_to_seven(test_db):
    for raw in ("x", "0", "-4", ""):
        put_setting(test_db, "due_ageing_days", raw)
        test_db.commit()
        assert invites.ageing_days(test_db) == 7


def test_ties_end_on_the_id_so_the_order_is_stable(test_db):
    a = _lead(test_db, "A", score=50)
    b = _lead(test_db, "B", score=50)
    for c in (a, b):
        c.created_at = None
    test_db.commit()
    first = [c.id for c in invites.due_new_leads(test_db)]
    assert first == sorted(first, key=str) or first == sorted(first)
    assert [c.id for c in invites.due_new_leads(test_db)] == first


def test_follow_ups_are_listed_whatever_the_invite_cap_and_never_cut_by_it(api_client, test_db):
    put_setting(test_db, "invites_daily_cap", "1")
    test_db.commit()
    for i in range(3):
        _lead(test_db, f"New{i}", score=90 - i)
    for i in range(3):
        _lead(test_db, f"Chase{i}", status="requested", score=1, due=False, invited_on=local_today() - timedelta(days=6))
    names = [r["name"] for r in api_client.get("/api/contacts", params={"due": True}).json()]
    assert {"Chase0", "Chase1", "Chase2"} <= set(names) and "New0" in names and "New1" not in names
