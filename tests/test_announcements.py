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
