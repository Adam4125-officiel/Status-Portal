"""Tests for per-user notifications: the queue, who gets told, and where from.

The three properties that matter most, and the reasons they matter:

- **Nothing is sent from the request that triggered it.** A slow SMTP server must not
  be able to make the admin panel hang.
- **Jellyfin-to-Seerr matching fails closed.** Guessing by email or username would
  eventually deliver one person's notifications to another.
- **Writing to Seerr happens only on an explicit button press.** It is the only call in
  this application that modifies another service.
"""
import json

import pytest

import app as app_module
import db
import discord_bot
import integrations
import notifications
import scheduler
import seerr_alerts
import user_notify


@pytest.fixture
def enabled(isolated_db):
    db.set_setting("user_notifications_enabled", "1")


# ---------------------------------------------------------------------------
# Queueing: cheap, and never a network call
# ---------------------------------------------------------------------------
def test_notifications_are_queued_not_sent(enabled, monkeypatch):
    """The whole design in one test: notify_user() writes a row and touches nothing
    outbound. Delivery is the scheduled task's job."""
    def explode(*a, **k):
        raise AssertionError("notify_user() must not send anything itself")

    monkeypatch.setattr(notifications, "send_email", explode)
    monkeypatch.setattr(discord_bot, "send_dm", explode)
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    assert db.notification_queue_summary()["pending"] == 1


def test_nothing_is_queued_while_the_feature_is_off(isolated_db):
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    assert db.notification_queue_summary()["pending"] == 0


def test_an_unknown_event_is_refused(enabled):
    """Events map to the preference that gates them, so an unrecognised one would be
    delivered ungated - refusing it is safer than defaulting to "send"."""
    with pytest.raises(ValueError):
        user_notify.notify_user("u1", "made_up_event", "s", "b")


def test_service_events_go_only_to_people_who_opted_in(enabled):
    """"Anything about services I use" defaults off - nobody wants a message for every
    maintenance window on every service."""
    db.set_user_preferences("wants", notify_service_events=True)
    db.set_user_preferences("doesnt", notify_service_events=False)
    user_notify.notify_service_subscribers("maintenance", "Maintenance", "Body")
    queued = {n["user_id"] for n in db.pending_notifications()}
    assert queued == {"wants"}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def test_delivery_uses_both_channels_when_both_are_set(enabled, monkeypatch):
    db.set_user_preferences("u1", notify_email="me@example.invalid", notify_discord_id="123")
    emails, dms = [], []
    monkeypatch.setattr(notifications, "send_email",
                        lambda s, b, recipients=None: emails.append(recipients) or True)
    monkeypatch.setattr(discord_bot, "send_dm", lambda uid, text: (dms.append(uid), (True, ""))[1])
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    user_notify.run_delivery_task()
    assert emails == [["me@example.invalid"]]
    assert dms == ["123"]
    assert db.notification_queue_summary()["sent"] == 1


def test_a_user_with_no_contact_details_is_not_retried_forever(enabled, monkeypatch):
    """"Nowhere to send it" is done, not failed. Retrying every two minutes for five
    attempts achieves nothing - no contact details will appear in the meantime."""
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    user_notify.run_delivery_task()
    assert db.notification_queue_summary() == {"pending": 0, "sent": 1, "failed": 0}


def test_the_preference_is_checked_at_delivery_time(enabled, monkeypatch):
    """So switching something off silences what's already queued, not just what comes
    next."""
    db.set_user_preferences("u1", notify_email="me@example.invalid", notify_own_reports=True)
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    db.set_user_preferences("u1", notify_own_reports=False)
    sent = []
    monkeypatch.setattr(notifications, "send_email",
                        lambda s, b, recipients=None: sent.append(s) or True)
    user_notify.run_delivery_task()
    assert sent == []


def test_a_failed_send_is_retried_then_given_up_on(enabled, monkeypatch):
    db.set_user_preferences("u1", notify_email="me@example.invalid")
    monkeypatch.setattr(notifications, "send_email", lambda s, b, recipients=None: False)
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    for _ in range(db.MAX_NOTIFICATION_ATTEMPTS):
        user_notify.run_delivery_task()
    summary = db.notification_queue_summary()
    assert summary["failed"] == 1 and summary["pending"] == 0


def test_partial_delivery_counts_as_sent(enabled, monkeypatch):
    """Retrying would re-deliver to the channel that already worked, which is worse
    than one missing copy on the other."""
    db.set_user_preferences("u1", notify_email="me@example.invalid", notify_discord_id="123")
    monkeypatch.setattr(notifications, "send_email", lambda s, b, recipients=None: True)
    monkeypatch.setattr(discord_bot, "send_dm", lambda uid, text: (False, "not connected"))
    user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    user_notify.run_delivery_task()
    assert db.notification_queue_summary()["sent"] == 1


def test_one_bad_row_does_not_stop_the_drain(enabled, monkeypatch):
    db.set_user_preferences("good", notify_email="ok@example.invalid")
    db.set_user_preferences("bad", notify_email="bad@example.invalid")
    user_notify.notify_user("bad", "report_reply", "First", "Body")
    user_notify.notify_user("good", "report_reply", "Second", "Body")

    def flaky(subject, body, recipients=None):
        if recipients == ["bad@example.invalid"]:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(notifications, "send_email", flaky)
    user_notify.run_delivery_task()
    assert db.notification_queue_summary()["sent"] == 1


def test_the_task_skips_rather_than_failing_when_switched_off(isolated_db):
    with pytest.raises(scheduler.TaskSkipped):
        user_notify.run_delivery_task()


def test_delivered_rows_are_eventually_pruned(enabled, monkeypatch):
    """This table would otherwise grow forever, exactly like status_history did."""
    db.set_user_preferences("u1", notify_email="me@example.invalid")
    monkeypatch.setattr(notifications, "send_email", lambda s, b, recipients=None: True)
    nid = user_notify.notify_user("u1", "report_reply", "Subject", "Body")
    user_notify.run_delivery_task()
    conn = db.get_db()
    conn.execute("UPDATE notification_queue SET sent_at='2020-01-01T00:00:00+00:00' WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    assert db.prune_notification_queue(days=30) == 1


# ---------------------------------------------------------------------------
# Matching a Jellyfin user to a Seerr user - the part that must fail closed
# ---------------------------------------------------------------------------
def _seerr_users(monkeypatch, users):
    monkeypatch.setattr(integrations, "fetch_seerr_users", lambda url, key, limit=200: users)


def test_a_real_jellyfin_link_is_followed(enabled, monkeypatch):
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [{"id": "3", "display_name": "Adam", "email": "adam@example.invalid",
                                 "discord_id": "999", "jellyfin_user_id": "u1"}])
    account = user_notify.find_seerr_account("u1")
    assert account["email"] == "adam@example.invalid"


def test_matching_never_falls_back_to_email_or_username(enabled, monkeypatch):
    """The failure this refusal prevents is sending one person's notifications to
    another. A Seerr user with no jellyfinUserId is simply not a match, however well
    the name lines up."""
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [{"id": "3", "display_name": "u1", "email": "u1@example.invalid",
                                 "discord_id": "", "jellyfin_user_id": ""}])
    assert user_notify.find_seerr_account("u1") is None


def test_an_unreachable_seerr_is_treated_as_no_link(enabled, monkeypatch):
    """"We couldn't check" and "there's no link" both correctly lead to asking the
    person directly, so they behave the same rather than erroring the page."""
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})

    def boom(url, key, limit=200):
        raise integrations.requests.RequestException("down")

    monkeypatch.setattr(integrations, "fetch_seerr_users", boom)
    assert user_notify.find_seerr_account("u1") is None


def test_contact_lookup_reads_stored_preferences_only(enabled, monkeypatch):
    """Delivery must not depend on Seerr being up - it runs once per queued message and
    a Seerr outage would otherwise stop notifications entirely."""
    monkeypatch.setattr(integrations, "fetch_seerr_users",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call Seerr")))
    db.set_user_preferences("u1", notify_email="stored@example.invalid")
    assert user_notify.contact_for("u1") == ("stored@example.invalid", "")


# ---------------------------------------------------------------------------
# Request progress
# ---------------------------------------------------------------------------
def _request(rid, status, by="3", title="Some Film"):
    return {"id": rid, "title": title, "media_type": "movie", "request_status": "Approved",
            "media_status_label": "x", "media_status": status, "requested_by": "Adam",
            "requested_by_id": by, "requested_at": "", "pending": False}


def test_a_request_becoming_available_notifies_whoever_asked(enabled, monkeypatch):
    db.set_user_preferences("u1", seerr_user_id="3", notify_email="me@example.invalid")
    integration = {"base_url": "http://s", "api_key": "k"}
    monkeypatch.setattr(integrations, "fetch_seerr_requests",
                        lambda url, key, limit=50: [_request(1, 3)])
    seerr_alerts.track_request_progress(integration)      # first sighting: processing
    monkeypatch.setattr(integrations, "fetch_seerr_requests",
                        lambda url, key, limit=50: [_request(1, 5)])
    assert seerr_alerts.track_request_progress(integration) == 1
    assert db.pending_notifications()[0]["user_id"] == "u1"


def test_a_first_sighting_of_something_already_available_says_nothing(enabled, monkeypatch):
    """Otherwise switching the feature on would announce the entire back catalogue."""
    db.set_user_preferences("u1", seerr_user_id="3", notify_email="me@example.invalid")
    monkeypatch.setattr(integrations, "fetch_seerr_requests",
                        lambda url, key, limit=50: [_request(1, 5)])
    assert seerr_alerts.track_request_progress({"base_url": "http://s", "api_key": "k"}) == 0


def test_an_unlinked_requester_is_not_notified(enabled, monkeypatch):
    """No Jellyfin<->Seerr link means no notification, rather than a guess."""
    integration = {"base_url": "http://s", "api_key": "k"}
    monkeypatch.setattr(integrations, "fetch_seerr_requests",
                        lambda url, key, limit=50: [_request(1, 3)])
    seerr_alerts.track_request_progress(integration)
    monkeypatch.setattr(integrations, "fetch_seerr_requests",
                        lambda url, key, limit=50: [_request(1, 5)])
    assert seerr_alerts.track_request_progress(integration) == 0


def test_request_states_survive_a_restart(enabled, monkeypatch):
    monkeypatch.setattr(integrations, "fetch_seerr_requests",
                        lambda url, key, limit=50: [_request(1, 3)])
    seerr_alerts.track_request_progress({"base_url": "http://s", "api_key": "k"})
    stored = json.loads(db.get_setting(seerr_alerts.REQUEST_STATES_SETTING))
    assert stored == {"1": 3}


# ---------------------------------------------------------------------------
# Where the triggers actually fire
# ---------------------------------------------------------------------------
def test_an_admin_reply_queues_a_notification(client, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    rid = db.create_problem_report("Something is broken", reporter_user="adam",
                                    reporter_user_id="u1")
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post(f"/admin/reports/{rid}/reply", data={"reply": "Looking into it"})
    queued = db.pending_notifications()
    assert len(queued) == 1
    assert queued[0]["user_id"] == "u1" and queued[0]["event"] == "report_reply"


def test_turning_a_report_into_an_incident_queues_one_too(client):
    db.set_setting("user_notifications_enabled", "1")
    rid = db.create_problem_report("Broken", reporter_user="adam", reporter_user_id="u1")
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post(f"/admin/reports/{rid}/create-incident")
    assert db.pending_notifications()[0]["event"] == "report_incident"


def test_an_anonymous_report_queues_nothing(client):
    """There's nobody to tell - reporter_user_id is empty, and enqueue_notification
    refuses a blank id rather than creating an undeliverable row."""
    db.set_setting("user_notifications_enabled", "1")
    rid = db.create_problem_report("Broken")
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post(f"/admin/reports/{rid}/reply", data={"reply": "Hello?"})
    assert db.pending_notifications() == []


# ---------------------------------------------------------------------------
# The account page: where a person sets this up for themselves
# ---------------------------------------------------------------------------
@pytest.fixture
def signed_in_visitor(client, monkeypatch):
    """Jellyfin sign-in switched on, one cached user, and that user signed in."""
    db.create_integration({"name": "Jellyfin", "kind": "jellyfin",
                            "base_url": "http://jellyfin.invalid", "api_key": "k", "enabled": 1})
    db.set_setting("jellyfin_auth_enabled", "1")
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    monkeypatch.setattr(app_module.jellyfin_auth, "authenticate",
                        lambda u, p: {"ok": True, "user": {"id": "u1", "name": "adam",
                                                            "is_administrator": False,
                                                            "is_disabled": False}})
    client.post("/login", data={"username": "adam", "password": "pw"})
    return client


def test_the_notification_settings_appear_only_when_the_admin_enabled_them(signed_in_visitor):
    assert b"Discord user ID" not in signed_in_visitor.get("/account").data
    db.set_setting("user_notifications_enabled", "1")
    assert b"Discord user ID" in signed_in_visitor.get("/account").data


def test_a_visitor_can_save_their_own_contact_details(signed_in_visitor):
    db.set_setting("user_notifications_enabled", "1")
    signed_in_visitor.post("/account", data={
        "theme": "auto", "contact": "",
        "notify_email": "me@example.invalid", "notify_discord_id": "123456789012345678",
        "notify_own_reports": "on"}, follow_redirects=True)
    prefs = db.get_user_preferences("u1")
    assert prefs["notify_email"] == "me@example.invalid"
    assert prefs["notify_discord_id"] == "123456789012345678"
    # Unticked boxes are absences, so these have to come back off.
    assert prefs["notify_service_events"] is False


def test_importing_from_seerr_copies_the_details_and_records_the_link(signed_in_visitor, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [{"id": "3", "display_name": "Adam",
                                 "email": "adam@example.invalid", "discord_id": "999",
                                 "jellyfin_user_id": "u1"}])
    signed_in_visitor.post("/account/seerr/import", follow_redirects=True)
    prefs = db.get_user_preferences("u1")
    assert prefs["notify_email"] == "adam@example.invalid"
    assert prefs["notify_discord_id"] == "999"
    assert prefs["seerr_user_id"] == "3"


def test_pushing_to_seerr_writes_only_that_users_two_contact_fields(signed_in_visitor, monkeypatch):
    """The only call in this application that modifies another service, so what it's
    allowed to touch is worth pinning down."""
    db.set_setting("user_notifications_enabled", "1")
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [{"id": "3", "display_name": "Adam", "email": "old@example.invalid",
                                 "discord_id": "", "jellyfin_user_id": "u1"}])
    db.set_user_preferences("u1", notify_email="new@example.invalid", notify_discord_id="42")
    pushed = []
    monkeypatch.setattr(integrations, "push_seerr_contact",
                        lambda url, key, uid, email=None, discord_id=None:
                        pushed.append((uid, email, discord_id)) or True)
    signed_in_visitor.post("/account/seerr/push", follow_redirects=True)
    assert pushed == [("3", "new@example.invalid", "42")]


def test_pushing_without_a_link_refuses_rather_than_guessing(signed_in_visitor, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [{"id": "3", "display_name": "adam", "email": "adam@example.invalid",
                                 "discord_id": "", "jellyfin_user_id": ""}])
    called = []
    monkeypatch.setattr(integrations, "push_seerr_contact",
                        lambda *a, **k: called.append(True))
    resp = signed_in_visitor.post("/account/seerr/push", follow_redirects=True)
    assert called == []
    assert b"Couldn" in resp.data


def test_the_seerr_endpoints_require_a_signed_in_visitor(client):
    for path in ("/account/seerr/import", "/account/seerr/push"):
        assert client.post(path).status_code == 302


def test_the_admin_page_never_lists_recipients(client):
    """The queue is addressed to a Jellyfin account; whose email or Discord ID that
    resolved to is that person's business, not something to print on an admin page."""
    db.set_setting("user_notifications_enabled", "1")
    db.set_user_preferences("u1", notify_email="private@example.invalid")
    user_notify.notify_user("u1", "report_reply", "A subject", "Body")
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/notifications/users").data
    assert b"A subject" in body
    assert b"private@example.invalid" not in body


# ---------------------------------------------------------------------------
# Contact details: Seerr owns them, the portal mirrors them, either side can fill them
# ---------------------------------------------------------------------------
def _linked_seerr(monkeypatch, email="", discord_id=""):
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [{"id": "7", "display_name": "Adam", "email": email,
                                 "discord_id": discord_id, "jellyfin_user_id": "u1"}])


def test_the_contact_sync_mirrors_only_linked_accounts(enabled, monkeypatch):
    """A Seerr user with no jellyfinUserId has nobody here to attach to. Matching by
    name or email would eventually give one person another's notifications."""
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://s",
                            "api_key": "k", "enabled": 1})
    _seerr_users(monkeypatch, [
        {"id": "7", "display_name": "Adam", "email": "adam@x", "discord_id": "1",
         "jellyfin_user_id": "u1"},
        {"id": "8", "display_name": "Nobody", "email": "no@x", "discord_id": "",
         "jellyfin_user_id": ""},
    ])
    message = user_notify.sync_seerr_contacts()
    assert db.get_seerr_contact("u1")["email"] == "adam@x"
    assert db.list_seerr_contacts().keys() == {"u1"}
    assert "1 linked" in message


def test_a_failed_contact_sync_leaves_the_previous_details_alone(enabled, monkeypatch):
    """Same rule as the Jellyfin user sync: an outage must not wipe what's known, or a
    blip would stop notifications for everyone."""
    _linked_seerr(monkeypatch, email="adam@x")
    user_notify.sync_seerr_contacts()

    def boom(url, key, limit=200):
        raise integrations.requests.RequestException("down")

    monkeypatch.setattr(integrations, "fetch_seerr_users", boom)
    with pytest.raises(integrations.requests.RequestException):
        user_notify.sync_seerr_contacts()
    assert db.get_seerr_contact("u1")["email"] == "adam@x"


def test_delivery_falls_back_to_the_synced_details(enabled, monkeypatch):
    """Somebody who filled their details in on Seerr and never touched this portal must
    still be reachable."""
    db.replace_seerr_contacts([{"jellyfin_user_id": "u1", "seerr_user_id": "7",
                                 "display_name": "Adam", "email": "from-seerr@x",
                                 "discord_id": "999"}])
    assert user_notify.contact_for("u1") == ("from-seerr@x", "999")


def test_saving_a_contact_writes_it_through_to_seerr(enabled, monkeypatch):
    """Seerr is the source of truth, so anything entered here goes there rather than
    becoming a private second copy that drifts."""
    _linked_seerr(monkeypatch)
    pushed = []
    monkeypatch.setattr(integrations, "push_seerr_contact",
                        lambda url, key, uid, email=None, discord_id=None:
                        pushed.append((uid, email, discord_id)) or True)
    ok, message = user_notify.save_contact("u1", email="new@x", discord_id="42")
    assert ok is True
    assert pushed == [("7", "new@x", "42")]
    # Mirrored locally straight away, so it works before the next sync runs.
    assert db.get_seerr_contact("u1")["email"] == "new@x"
    assert user_notify.contact_for("u1") == ("new@x", "42")


def test_a_failed_write_back_still_keeps_what_was_typed(enabled, monkeypatch):
    """Losing somebody's input because another service was unreachable is the worse
    outcome - so it's reported, not rolled back."""
    _linked_seerr(monkeypatch)

    def boom(*a, **k):
        raise integrations.requests.RequestException("seerr down")

    monkeypatch.setattr(integrations, "push_seerr_contact", boom)
    ok, message = user_notify.save_contact("u1", email="typed@x")
    assert ok is False
    assert "Seerr couldn" in message
    assert user_notify.contact_for("u1")[0] == "typed@x"


def test_saving_without_a_linked_seerr_account_still_works(enabled):
    """No Seerr, or no link, must not stop somebody being reachable by this portal."""
    ok, message = user_notify.save_contact("u1", email="local@x")
    assert ok is True and "nowhere to copy it to" in message
    assert user_notify.contact_for("u1")[0] == "local@x"


# ---------------------------------------------------------------------------
# The two places a missing detail can be filled in
# ---------------------------------------------------------------------------
def test_the_admin_can_set_a_users_contact_details(client, isolated_db, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    _linked_seerr(monkeypatch)
    pushed = []
    monkeypatch.setattr(integrations, "push_seerr_contact",
                        lambda url, key, uid, email=None, discord_id=None:
                        pushed.append(email) or True)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/users/u1/contact",
                 data={"notify_email": "set-by-admin@x", "notify_discord_id": ""},
                 follow_redirects=True)
    assert pushed == ["set-by-admin@x"]
    assert user_notify.contact_for("u1")[0] == "set-by-admin@x"


def test_the_admin_contact_form_only_shows_when_notifications_are_on(client, isolated_db):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    assert b"contact-form" not in client.get("/admin/users").data
    db.set_setting("user_notifications_enabled", "1")
    assert b"contact-form" in client.get("/admin/users").data


def test_a_visitor_with_nowhere_to_be_reached_is_prompted(signed_in_visitor):
    db.set_setting("user_notifications_enabled", "1")
    assert user_notify.needs_contact_details("u1") is True
    resp = signed_in_visitor.get("/account/contact")
    assert resp.status_code == 200
    assert b"Where should we reach you" in resp.data


def test_the_prompt_is_skippable_and_stays_skipped(signed_in_visitor):
    """Asking the same question on every sign-in is how a prompt becomes something
    people learn to click past without reading."""
    db.set_setting("user_notifications_enabled", "1")
    signed_in_visitor.post("/account/contact", data={"skip": "1"}, follow_redirects=True)
    assert db.get_user_preferences("u1")["contact_prompt_dismissed"] is True
    assert user_notify.needs_contact_details("u1") is False
    # And it stops rendering.
    assert signed_in_visitor.get("/account/contact").status_code == 302


def test_a_visitor_who_already_has_details_is_not_prompted(signed_in_visitor):
    db.set_setting("user_notifications_enabled", "1")
    db.set_user_preferences("u1", notify_email="already@x")
    assert user_notify.needs_contact_details("u1") is False


def test_nobody_is_prompted_while_the_feature_is_off(signed_in_visitor):
    assert user_notify.needs_contact_details("u1") is False


def test_answering_the_prompt_saves_and_stops_asking(signed_in_visitor, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    _linked_seerr(monkeypatch)
    monkeypatch.setattr(integrations, "push_seerr_contact", lambda *a, **k: True)
    signed_in_visitor.post("/account/contact", data={"notify_email": "mine@x"},
                            follow_redirects=True)
    assert user_notify.contact_for("u1")[0] == "mine@x"
    assert db.get_user_preferences("u1")["contact_prompt_dismissed"] is True


def test_the_prompt_endpoint_requires_a_signed_in_visitor(client, isolated_db):
    assert client.get("/account/contact").status_code == 302
