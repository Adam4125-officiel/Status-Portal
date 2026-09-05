"""Announcements pushed out as notifications: the send history table, the email fan-out
through the existing per-user queue, and the one-shot Discord channel post.

Three things worth keeping straight while reading these:

- **Extends the existing announcements feature, doesn't replace it.** An announcement
  is still just a public banner unless the admin explicitly sends it - creating or
  editing one with no channels checked behaves exactly as it always has.
- **Email fans out per-user through notify_service_subscribers()**, respecting each
  user's own notify_email_announcements preference - nothing new here beyond a new
  event key. **Discord is one post to one configured channel**, never a DM broadcast.
- **A send is recorded immediately**, before the (possibly slow, backgrounded) Discord
  post resolves - so the history page always reflects that a send happened, even while
  Discord is still in flight.
"""
import app as app_module
import db
import discord_bot
import user_notify


# ---------------------------------------------------------------------------
# db.py accessors
# ---------------------------------------------------------------------------
def test_create_announcement_returns_the_new_id(isolated_db):
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    assert db.get_announcement(aid)["title"] == "Heads up"


def test_record_and_list_announcement_sends(isolated_db):
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    send_id = db.record_announcement_send(aid, "Heads up", "email,discord", recipient_count=3)
    sends = db.list_announcement_sends()
    assert len(sends) == 1
    assert sends[0]["id"] == send_id
    assert sends[0]["recipient_count"] == 3
    assert sends[0]["discord_detail"] == ""  # not yet filled in


def test_set_announcement_send_detail_fills_in_the_discord_outcome(isolated_db):
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    send_id = db.record_announcement_send(aid, "Heads up", "discord")
    db.set_announcement_send_detail(send_id, "Posted.")
    assert db.list_announcement_sends()[0]["discord_detail"] == "Posted."


def test_announcement_sends_survive_the_announcement_being_deleted(isolated_db):
    """ON DELETE SET NULL, not CASCADE - the history is a record of what was sent, and
    must outlive the announcement it was sent from, the same reasoning problem_reports
    keeps a deleted incident's row readable instead of losing the report."""
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    db.record_announcement_send(aid, "Heads up", "email", recipient_count=2)
    db.delete_announcement(aid)
    sends = db.list_announcement_sends()
    assert len(sends) == 1
    assert sends[0]["announcement_id"] is None


def test_notify_email_announcements_defaults_on(isolated_db):
    assert db.get_user_preferences("u1")["notify_email_announcements"] is True


# ---------------------------------------------------------------------------
# user_notify.py event mapping
# ---------------------------------------------------------------------------
def test_announcement_email_respects_the_per_user_preference(isolated_db, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    db.set_user_preferences("wants", notify_email="wants@example.invalid",
                            notify_email_announcements=True)
    db.set_user_preferences("declined", notify_email="declined@example.invalid",
                            notify_email_announcements=False)
    queued = user_notify.notify_service_subscribers("announcement", "Heads up", "test")
    assert queued == 1
    assert db.pending_notifications()[0]["user_id"] == "wants"


def test_announcement_has_no_per_user_discord_preference(isolated_db):
    """Discord is a single channel post, not a DM - there is nothing per-user to gate,
    so the mapping is None exactly like seerr_event's email slot, for a different
    reason (see the comment in EVENT_CHANNEL_PREFERENCE)."""
    assert user_notify.EVENT_CHANNEL_PREFERENCE["announcement"]["discord"] is None


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})


def test_creating_with_no_channels_checked_behaves_as_before(client):
    """The additive guarantee: nothing about plain create/edit changes when the admin
    doesn't ask for a send."""
    _login(client)
    client.post("/admin/announcements/new", data={"title": "Heads up", "message": "test"})
    assert len(db.list_announcements()) == 1
    assert db.list_announcement_sends() == []


def test_creating_with_email_checked_queues_and_records_a_send(client, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    db.set_user_preferences("u1", notify_email="me@example.invalid")
    _login(client)
    client.post("/admin/announcements/new",
               data={"title": "Heads up", "message": "test", "channels": "email"})
    assert db.notification_queue_summary()["pending"] == 1
    sends = db.list_announcement_sends()
    assert len(sends) == 1 and sends[0]["channels"] == "email" and sends[0]["recipient_count"] == 1


def test_send_route_requires_at_least_one_channel(client):
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    _login(client)
    resp = client.post(f"/admin/announcements/{aid}/send", data={}, follow_redirects=True)
    assert b"Choose at least one channel" in resp.data
    assert db.list_announcement_sends() == []


def test_send_route_404_message_for_unknown_announcement(client):
    _login(client)
    resp = client.post("/admin/announcements/999/send", data={"channels": "email"},
                       follow_redirects=True)
    assert b"Announcement not found" in resp.data


def test_send_route_with_no_discord_channel_configured_reports_it(client):
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    _login(client)
    resp = client.post(f"/admin/announcements/{aid}/send", data={"channels": "discord"},
                       follow_redirects=True)
    assert b"no announcement channel configured" in resp.data
    sends = db.list_announcement_sends()
    assert sends[0]["discord_detail"] == "No announcement channel configured."


def test_send_route_dispatches_discord_in_the_background(client, monkeypatch):
    """The route must return without waiting on the Discord call - proven here by
    making send_channel_message() block until the test releases it, and asserting the
    HTTP response already came back."""
    import threading
    released = threading.Event()
    calls = []

    def _slow_send(channel_id, text):
        calls.append((channel_id, text))
        released.wait(timeout=2)
        return True, ""

    db.set_setting("discordbot_announcement_channel_id", "555")
    monkeypatch.setattr(discord_bot, "send_channel_message", _slow_send)
    aid = db.create_announcement({"title": "Heads up", "message": "Doing maintenance."})
    _login(client)
    resp = client.post(f"/admin/announcements/{aid}/send", data={"channels": "discord"})
    assert resp.status_code == 302  # the route already returned
    released.set()
    import time
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [("555", "**Heads up**\nDoing maintenance.")]


def test_send_route_records_success_after_discord_post_completes(client, monkeypatch):
    import time
    db.set_setting("discordbot_announcement_channel_id", "555")
    monkeypatch.setattr(discord_bot, "send_channel_message", lambda cid, text: (True, ""))
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    _login(client)
    client.post(f"/admin/announcements/{aid}/send", data={"channels": "discord"})
    deadline = time.time() + 2
    while db.list_announcement_sends()[0]["discord_detail"] == "" and time.time() < deadline:
        time.sleep(0.01)
    assert db.list_announcement_sends()[0]["discord_detail"] == "Posted."


def test_send_route_ignores_an_unrecognised_channel(client):
    """channels is whitelisted against ('email', 'discord') server-side, not trusted
    from the form - the same reasoning as every other server-checked whitelist here."""
    aid = db.create_announcement({"title": "Heads up", "message": "test"})
    _login(client)
    resp = client.post(f"/admin/announcements/{aid}/send",
                       data={"channels": "sms"}, follow_redirects=True)
    assert b"Choose at least one channel" in resp.data


# ---------------------------------------------------------------------------
# The display window (added 1.8.7)
# ---------------------------------------------------------------------------
def test_an_announcement_with_no_window_behaves_exactly_as_before(isolated_db):
    """The property the whole feature has to preserve. Both fields blank is what every
    announcement written before this existed has, and what a new one gets unless the
    admin fills something in."""
    db.create_announcement({"title": "Always", "message": "m"})
    assert [a["title"] for a in db.list_active_announcements()] == ["Always"]
    assert db.announcement_window_state(db.list_announcements()[0]) == "showing"


def test_a_scheduled_announcement_is_not_public_until_it_starts(isolated_db):
    db.create_announcement({"title": "Later", "message": "m", "starts_at": "2099-01-01T00:00"})
    assert db.list_active_announcements() == []
    assert db.announcement_window_state(db.list_announcements()[0]) == "scheduled"


def test_an_expired_announcement_stops_being_public(isolated_db):
    db.create_announcement({"title": "Over", "message": "m", "ends_at": "2000-01-01T00:00"})
    assert db.list_active_announcements() == []
    assert db.announcement_window_state(db.list_announcements()[0]) == "expired"


def test_the_admin_list_still_shows_every_announcement(client, isolated_db):
    """It is the only page that can edit a scheduled or expired one, so filtering there
    would strand them with no way back."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.create_announcement({"title": "Later", "message": "m", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "Over", "message": "m", "ends_at": "2000-01-01T00:00"})
    db.create_announcement({"title": "Live", "message": "m"})

    body = client.get("/admin/announcements").get_data(as_text=True)

    assert "Later" in body and "Over" in body and "Live" in body


def test_expiry_is_decided_at_read_time_with_no_background_job(isolated_db):
    """No stored flag to flip, so nothing has to catch up after downtime and extending
    an expired announcement brings it straight back - rather than the admin having to
    remember to switch it on again."""
    aid = db.create_announcement({"title": "Over", "message": "m", "ends_at": "2000-01-01T00:00"})
    assert db.list_active_announcements() == []

    db.update_announcement(aid, {"title": "Over", "message": "m", "ends_at": "2099-01-01T00:00"})
    assert [a["title"] for a in db.list_active_announcements()] == ["Over"]


def test_a_window_that_can_never_open_is_refused(client, isolated_db):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/announcements/new",
                       data={"title": "Backwards", "message": "m",
                             "starts_at": "2026-06-01T10:00", "ends_at": "2026-06-01T09:00"},
                       follow_redirects=True)
    assert "has to be after its start" in resp.get_data(as_text=True)
    assert db.list_announcements() == []


def test_a_rejected_new_announcement_still_reads_as_new_and_keeps_what_was_typed(
        client, isolated_db):
    """`announcement` carries the field values on a rejected submit, so nothing typed is
    lost - which is why the page's identity has to come from something else."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.post("/admin/announcements/new",
                       data={"title": "Kept text", "message": "m",
                             "starts_at": "2026-06-01T10:00", "ends_at": "2026-06-01T09:00"},
                       ).get_data(as_text=True)
    assert "New announcement" in body and "Edit announcement" not in body
    assert "Kept text" in body


def test_sending_a_scheduled_announcement_is_refused(client, isolated_db, monkeypatch):
    """A notification would point people at something they can't see yet."""
    sent = []
    monkeypatch.setattr(user_notify, "notify_service_subscribers",
                        lambda *a, **k: sent.append(a) or 1)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("user_notifications_enabled", "1")
    aid = db.create_announcement({"title": "Later", "message": "m", "starts_at": "2099-01-01T00:00"})

    resp = client.post(f"/admin/announcements/{aid}/send", data={"channels": "email"},
                       follow_redirects=True)

    # "isn't" renders HTML-escaped, so match on the unambiguous half.
    assert "showing yet" in resp.get_data(as_text=True)
    assert sent == []
    assert db.list_announcement_sends() == []


def test_sending_an_expired_announcement_is_refused(client, isolated_db, monkeypatch):
    sent = []
    monkeypatch.setattr(user_notify, "notify_service_subscribers",
                        lambda *a, **k: sent.append(a) or 1)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("user_notifications_enabled", "1")
    aid = db.create_announcement({"title": "Over", "message": "m", "ends_at": "2000-01-01T00:00"})

    resp = client.post(f"/admin/announcements/{aid}/send", data={"channels": "email"},
                       follow_redirects=True)

    assert "has expired" in resp.get_data(as_text=True)
    assert sent == []


def test_a_currently_showing_announcement_still_sends(client, isolated_db, monkeypatch):
    """The guard must not get in the way of the ordinary case."""
    sent = []
    monkeypatch.setattr(user_notify, "notify_service_subscribers",
                        lambda *a, **k: sent.append(a) or 3)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("user_notifications_enabled", "1")
    aid = db.create_announcement({"title": "Now", "message": "m",
                                   "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00"})

    client.post(f"/admin/announcements/{aid}/send", data={"channels": "email"})

    assert len(sent) == 1
    assert len(db.list_announcement_sends()) == 1


# Every public-facing surface has to filter, not just the status page. Each of these is
# a separate call site, and a missed one is a leak: a scheduled announcement is
# unpublished content, so it appearing in a feed or a bot embed is worse than an expired
# one lingering.
def test_the_public_page_only_shows_active_announcements(client, isolated_db):
    db.create_announcement({"title": "ScheduledOne", "message": "m", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "ExpiredOne", "message": "m", "ends_at": "2000-01-01T00:00"})
    db.create_announcement({"title": "LiveOne", "message": "m"})

    body = client.get("/").get_data(as_text=True)

    assert "LiveOne" in body
    assert "ScheduledOne" not in body and "ExpiredOne" not in body


def test_the_json_api_only_shows_active_announcements(client, isolated_db):
    db.create_announcement({"title": "ScheduledOne", "message": "m", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "LiveOne", "message": "m"})

    titles = [a["title"] for a in client.get("/api/status").get_json()["announcements"]]

    assert titles == ["LiveOne"]


def test_the_rss_feed_only_shows_active_announcements(client, isolated_db):
    db.create_announcement({"title": "ScheduledOne", "message": "m", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "LiveOne", "message": "m"})

    body = client.get("/feed.xml").get_data(as_text=True)

    assert "LiveOne" in body and "ScheduledOne" not in body


def test_the_discord_embed_only_shows_active_announcements(isolated_db):
    db.create_announcement({"title": "ScheduledOne", "message": "m", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "LiveOne", "message": "m"})

    data = discord_bot.build_status_data({"services": False, "incidents": False,
                                           "announcements": True, "maintenance": False,
                                           "resources": {}})
    rendered = repr(data)

    assert "LiveOne" in rendered and "ScheduledOne" not in rendered


def test_the_kiosk_only_shows_active_announcements(isolated_db):
    """And with nothing active the view is skipped entirely rather than rendering an
    empty panel onto somebody's wall - the kiosk rule for every view."""
    db.create_announcement({"title": "ScheduledOne", "message": "m", "starts_at": "2099-01-01T00:00"})
    assert app_module._kiosk_announcements_context() is None

    db.create_announcement({"title": "LiveOne", "message": "m"})
    context = app_module._kiosk_announcements_context()
    assert [a["title"] for a in context["announcements"]] == ["LiveOne"]
