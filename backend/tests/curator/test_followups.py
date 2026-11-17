"""Follow-ups at day 4 and day 9, then closed (curator/followups.py)."""
from datetime import timedelta

import pytest

from backend.accounts.settings import put_setting
from backend.curator import followups, invites
from backend.models.db import Contact
from backend.tz import local_today

TODAY = local_today()


def _lead(db, status="requested", days_ago=0, **kw):
    start = TODAY - timedelta(days=days_ago)
    c = Contact(name=kw.pop("name", "A"), company="Acme", linkedin_url=kw.pop("url", "u"), status=status,
                contacted_on=start, invited_on=start if status == "requested" else None, found_on=start, **kw)
    db.add(c)
    db.commit()
    return c


@pytest.mark.parametrize("raw,expected", [("2", 2), (" 3 ", 3), ("0", 0), ("5", 5), ("9", 5), ("-1", 2), ("abc", 2),
                                          ("", 2), (None, 2), ("1.5", 2)])
def test_parse_max_falls_back_to_the_default(raw, expected):
    assert followups.parse_max(raw) == expected


@pytest.mark.parametrize("raw,expected", [("4,9", (4, 9)), (" 3 , 7 , 14", (3, 7, 14)), ("5", (5,)), ("9,4", (4, 9)[::-1] and (4, 9)),
                                          ("4,4", (4, 9)), ("0,5", (4, 9)), ("a,b", (4, 9)), ("", (4, 9)), (None, (4, 9)),
                                          ("4,,9", (4, 9)), ("4,900", (4, 9)), ("-2,5", (4, 9))])
def test_parse_days_falls_back_to_the_default(raw, expected):
    assert followups.parse_days(raw) == expected


def test_schedule_reads_the_settings_and_cuts_the_count_to_the_days(test_db):
    assert followups.schedule(test_db) == (2, (4, 9), 5, 8)
    put_setting(test_db, "followup_max", "3")
    put_setting(test_db, "followup_days", "2,5")
    put_setting(test_db, "followup_close_after_days", "3")
    put_setting(test_db, "followup_daily_limit", "4")
    test_db.commit()
    assert followups.schedule(test_db) == (2, (2, 5), 3, 4)
    put_setting(test_db, "followup_days", "nonsense")
    put_setting(test_db, "followup_max", "x")
    put_setting(test_db, "followup_close_after_days", "-2")
    put_setting(test_db, "followup_daily_limit", "0")
    test_db.commit()
    assert followups.schedule(test_db) == (2, (4, 9), 5, 8)


@pytest.mark.parametrize("raw,expected", [("5", 5), ("0", 0), ("60", 60), ("61", 5), ("-1", 5), ("x", 5), (None, 5), ("", 5)])
def test_close_after_falls_back_to_the_default(raw, expected):
    assert followups.parse_close_after(raw) == expected


def test_a_negative_max_counts_as_the_default_not_as_off():
    assert followups.parse_max("-1") == followups.DEFAULT_MAX == 2 and followups.parse_max("0") == 0


def test_follow_up_1_is_due_on_day_4_and_2_on_day_9(test_db):
    c = _lead(test_db, days_ago=3)
    assert followups.info(c)["due"] is False and followups.info(c)["label"].startswith("follow-up 1 of 2 on")
    c.contacted_on = TODAY - timedelta(days=4)
    i = followups.info(c)
    assert i["due"] and i["n"] == 1 and i["of"] == 2 and i["label"] == "follow-up 1 of 2 due"
    followups.record_sent(c)
    assert c.follow_ups_sent == 1 and c.next_action_at is None
    assert followups.info(c, TODAY + timedelta(days=4))["due"] is False         # 8 days after the first contact
    c.contacted_on = TODAY - timedelta(days=9)
    c.follow_up_sent_on = TODAY - timedelta(days=1)
    i = followups.info(c)
    assert i["due"] and i["n"] == 2 and i["label"] == "follow-up 2 of 2 due"


def test_the_gap_is_counted_from_the_first_contact_not_from_the_last_follow_up(test_db):
    c = _lead(test_db, days_ago=6)
    followups.record_sent(c)
    assert followups.info(c)["due_on"] == (TODAY - timedelta(days=6) + timedelta(days=9)).isoformat()


def _send_all(c, days_ago_first=12):
    """Record both follow-ups on the days they were due."""
    followups.record_sent(c, today=TODAY - timedelta(days=days_ago_first - 4))
    followups.record_sent(c, today=TODAY - timedelta(days=days_ago_first - 9))


def test_after_the_maximum_the_lead_waits_then_is_closed_and_stops_being_due(test_db):
    c = _lead(test_db, days_ago=12)
    _send_all(c)                                                     # the last one went out 3 days ago
    i = followups.info(c)
    assert i["waiting"] and not i.get("closed") and i["due"] is False        # a reply still has time to come
    followups.mark_due(test_db, TODAY)
    test_db.refresh(c)
    assert c.status == "requested"
    later = TODAY + timedelta(days=2)                                # 5 days after the last follow-up
    assert followups.info(c, later)["closed"]
    followups.mark_due(test_db, later)
    test_db.refresh(c)
    assert c.status == "skip" and c.message_stage == "closed_no_reply" and c.next_action_at is None
    assert followups.mark_due(test_db, later) == 0


def test_the_close_wait_is_a_setting(test_db):
    put_setting(test_db, "followup_close_after_days", "1")
    test_db.commit()
    c = _lead(test_db, days_ago=12)
    _send_all(c)
    sched = followups.schedule(test_db)
    assert followups.info(c, TODAY, sched)["closed"]                 # the last one was 3 days ago, the wait is 1


def test_a_second_record_the_same_day_is_the_same_follow_up(test_db):
    c = _lead(test_db, days_ago=5)
    assert followups.record_sent(c) == 1
    assert followups.record_sent(c) == 1 and c.follow_ups_sent == 1       # a double click or a retry
    assert followups.record_sent(c, today=TODAY) == 1


def test_recording_is_refused_when_nothing_is_due(test_db):
    early = _lead(test_db, days_ago=2, url="e")
    with pytest.raises(ValueError, match="not due until"):
        followups.record_sent(early)
    assert early.follow_ups_sent in (0, None)
    done = _lead(test_db, days_ago=12, url="d")
    _send_all(done)
    with pytest.raises(ValueError, match="already sent"):
        followups.record_sent(done, today=TODAY)
    other = _lead(test_db, "connected", url="c")
    with pytest.raises(ValueError, match="not waiting"):
        followups.record_sent(other)


def test_a_lead_open_since_before_the_schedule_and_long_past_it_is_closed_without_a_flood(test_db):
    old = _lead(test_db, days_ago=20, url="old")                    # past the last day (9) plus the wait (5)
    assert followups.info(old)["closed"] and followups.is_due(old) is False
    fresh = _lead(test_db, days_ago=14, url="f")
    assert followups.info(fresh)["due"]                             # 14 days is still inside the window
    followups.mark_due(test_db, TODAY)
    test_db.refresh(old)
    assert old.status == "skip" and old.message_stage == "closed_no_reply"


def test_the_due_list_shows_at_most_the_daily_limit_of_follow_ups(test_db):
    put_setting(test_db, "followup_daily_limit", "3")
    test_db.commit()
    for i in range(6):
        _lead(test_db, days_ago=5 + (i % 3), url=f"u{i}", name=f"L{i}")
    _lead(test_db, "connected", url="acc", name="Accepted")          # an acceptance is not a follow-up: never cut
    names = [c.name for c in followups.due_leads(test_db)]
    assert len(names) == 4 and "Accepted" in names
    done = test_db.query(Contact).filter(Contact.name == names[0]).first()
    followups.record_sent(done)
    test_db.commit()
    assert len([c for c in followups.due_leads(test_db) if c.status == "requested"]) == 2     # 3 a day, one already done


def test_reading_the_due_list_writes_nothing(test_db, api_client):
    from backend.connector import tools
    c = _lead(test_db, days_ago=5)
    old = _lead(test_db, days_ago=30, url="o", name="Old")
    before = [(x.status, x.next_action_at, x.message_stage, x.follow_ups_sent) for x in test_db.query(Contact).order_by(Contact.name)]
    rows = api_client.get("/api/contacts", params={"due": True}).json()
    assert [r["name"] for r in rows] == ["A"]
    assert tools.list_leads(test_db, {"due": True})["total"] == 1
    tools.today(test_db, {})
    test_db.expire_all()
    assert before == [(x.status, x.next_action_at, x.message_stage, x.follow_ups_sent) for x in test_db.query(Contact).order_by(Contact.name)]
    followups.mark_due(test_db, TODAY)                                # the scheduled job is what writes
    test_db.refresh(old)
    test_db.refresh(c)
    assert old.status == "skip" and c.next_action_at == TODAY


def test_the_first_contact_day_uses_the_app_day_not_the_utc_day(test_db):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from backend.tz import APP_TZ
    c = Contact(name="Z", company="Acme", linkedin_url="z", status="messaged", found_on=TODAY - timedelta(days=30))
    # 23:30 local on 5 days ago is already the next day in UTC
    moment = datetime.combine(TODAY - timedelta(days=5), datetime.min.time().replace(hour=23, minute=30), tzinfo=APP_TZ)
    c.last_action_at = moment.astimezone(timezone.utc)
    assert followups.anchor(c) == TODAY - timedelta(days=5)


def test_the_contacted_on_backfill_is_idempotent_and_reads_the_right_dates(test_db):
    from datetime import datetime, timezone
    from sqlalchemy import text
    from backend.seed import contacted_on_backfill_sql
    when = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)              # still Nov 2 in New York
    test_db.add_all([
        Contact(name="R1", company="A", linkedin_url="1", status="requested", invited_on=TODAY - timedelta(days=6), last_action_at=when),
        Contact(name="R2", company="A", linkedin_url="2", status="requested", last_action_at=when),
        Contact(name="M1", company="A", linkedin_url="3", status="messaged", invited_on=TODAY - timedelta(days=20), last_action_at=when),
        Contact(name="M2", company="A", linkedin_url="4", status="messaged", invited_on=TODAY - timedelta(days=9)),
        Contact(name="C1", company="A", linkedin_url="5", status="connected", last_action_at=when),
        Contact(name="R3", company="A", linkedin_url="6", status="requested", contacted_on=TODAY),
        Contact(name="N1", company="A", linkedin_url="7", status="requested")])
    test_db.commit()
    for _ in range(2):                                                  # run twice: same result
        test_db.execute(text(contacted_on_backfill_sql("sqlite")))
        test_db.commit()
    got = {c.name: c.contacted_on for c in test_db.query(Contact).all()}
    assert got["R1"] == TODAY - timedelta(days=6) and got["R2"].isoformat() in ("2026-11-02", "2026-11-03")
    assert got["M1"].isoformat() in ("2026-11-02", "2026-11-03") and got["M2"] == TODAY - timedelta(days=9)
    assert got["C1"] is None and got["R3"] == TODAY and got["N1"] is None


def test_mark_due_puts_due_follow_ups_on_todays_list_only(test_db):
    due = _lead(test_db, days_ago=4, url="u1", name="Due")
    early = _lead(test_db, days_ago=2, url="u2", name="Early")
    msg = _lead(test_db, "messaged", days_ago=10, url="u3", name="Msg")
    assert followups.mark_due(test_db, TODAY) == 2
    for c in (due, early, msg):
        test_db.refresh(c)
    assert due.next_action_at == TODAY and msg.next_action_at == TODAY and early.next_action_at is None


@pytest.mark.parametrize("status", ["connected", "replied", "referral", "skip", "snoozed", "new"])
def test_a_reply_or_acceptance_or_any_other_status_is_not_in_the_sequence(test_db, status):
    c = _lead(test_db, status, days_ago=20)
    assert followups.info(c) is None


def test_accepting_stops_the_sequence_and_makes_the_message_due(test_db, api_client):
    c = _lead(test_db, days_ago=5)
    r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "connected"}).json()
    assert r["follow_up"] is None and r["message_stage"] == "after_acceptance"
    assert followups.mark_due(test_db, TODAY) == 1            # the first message after acceptance is due
    test_db.refresh(c)
    assert c.next_action_at == TODAY and c.status == "connected"
    r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "replied"}).json()
    assert r["follow_up"] is None and r["message_stage"] is None


def test_setting_followup_max_zero_turns_them_off(test_db):
    put_setting(test_db, "followup_max", "0")
    test_db.commit()
    c = _lead(test_db, days_ago=30)
    sched = followups.schedule(test_db)
    assert followups.info(c, sched=sched) is None
    followups.mark_due(test_db, TODAY)
    test_db.refresh(c)
    assert c.status == "requested"


def test_a_new_message_starts_a_new_sequence(test_db):
    c = _lead(test_db, days_ago=9)
    followups.record_sent(c)
    c.status = "messaged"
    followups.on_status_change(c, "messaged", TODAY)
    assert c.follow_ups_sent == 0 and c.contacted_on == TODAY and c.message_stage == "follow_up_1"


def test_follow_ups_do_not_count_against_the_invite_cap(test_db, api_client):
    c = _lead(test_db, days_ago=4)
    before = invites.state(test_db)
    r = api_client.patch(f"/api/contacts/{c.id}", json={"follow_up_sent": True})
    assert r.status_code == 200 and r.json()["follow_ups_sent"] == 1
    assert r.json()["invites"] == before == invites.state(test_db)
    test_db.refresh(c)
    assert c.invited_on == TODAY - timedelta(days=4)            # untouched


def test_recording_a_follow_up_for_a_lead_not_in_a_sequence_is_refused(test_db, api_client):
    c = _lead(test_db, "connected")
    assert api_client.patch(f"/api/contacts/{c.id}", json={"follow_up_sent": True}).status_code == 409


def test_the_due_label_is_in_the_api_and_the_tools(test_db, api_client):
    from backend.connector import tools
    c = _lead(test_db, days_ago=5)
    rows = api_client.get("/api/contacts", params={"due": True}).json()
    assert rows[0]["follow_up"]["label"] == "follow-up 1 of 2 due"
    leads = tools.list_leads(test_db, {"due": True})["leads"]
    assert leads[0]["follow_up"] == "follow-up 1 of 2 due"
    assert tools.today(test_db, {})["follow_ups_due"][0]["follow_up"] == "follow-up 1 of 2 due"
    out = tools.update_lead(test_db, {"lead_id": str(c.id), "follow_up_sent": True})
    assert out["follow_ups_sent"] == 1


def test_log_lead_and_patch_start_the_sequence(test_db):
    from backend.curator import leads
    lead, _ = leads.log_lead(test_db, {"name": "Zed", "status": "requested", "contacted_on": TODAY - timedelta(days=5)})
    assert lead.contacted_on == TODAY - timedelta(days=5) and followups.info(lead)["due"]


def test_a_double_click_on_follow_up_sent_through_the_api_records_one(test_db, api_client):
    c = _lead(test_db, days_ago=5)
    first = api_client.patch(f"/api/contacts/{c.id}", json={"follow_up_sent": True})
    second = api_client.patch(f"/api/contacts/{c.id}", json={"follow_up_sent": True})
    assert first.status_code == second.status_code == 200
    assert first.json()["follow_ups_sent"] == second.json()["follow_ups_sent"] == 1
    assert second.json()["follow_up"]["n"] == 2                      # the second follow-up is still to come, on day 9
    early = _lead(test_db, days_ago=1, url="e2", name="Early")
    assert api_client.patch(f"/api/contacts/{early.id}", json={"follow_up_sent": True}).status_code == 409


def test_status_moves_remember_the_text_sent(test_db, api_client):
    c = Contact(name="K", company="Acme", linkedin_url="k", status="new", note="Hi K, the note", found_on=TODAY)
    test_db.add(c)
    test_db.commit()
    r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "requested"}).json()
    test_db.refresh(c)
    assert c.last_sent_text == "Hi K, the note" and r["message_stage"] == "follow_up_1"
