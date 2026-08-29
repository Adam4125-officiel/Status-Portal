"""Tests for the Seerr approval alert: the pending count, and the Discord DM path.

The DM half is exercised with fake discord.py objects, exactly like the rest of
tests/test_discord_bot.py - there is no Discord gateway in this sandbox. What that
does prove is the threading bridge, the edge-triggering, and the failure handling;
what it cannot prove is that Discord actually delivers a DM. See docs/HISTORY.md.
"""
import json

import pytest

import db
import discord_bot
import integrations
import scheduler
import seerr_alerts


# ---------------------------------------------------------------------------
# The DM path in discord_bot
# ---------------------------------------------------------------------------
class _FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class _FakeClient:
    def __init__(self, user=None, raises=None):
        self._user = user or _FakeUser()
        self._raises = raises

    def get_user(self, uid):
        return None          # force the fetch_user path, the cold-cache case

    async def fetch_user(self, uid):
        if self._raises:
            raise self._raises
        return self._user


class _ImmediateLoop:
    """Stands in for the bot's asyncio loop. run_coroutine_threadsafe is patched to
    drive the coroutine synchronously, so the test asserts on what the coroutine did
    rather than on scheduling machinery."""


def _patch_runtime(monkeypatch, client, discord_module=None):
    monkeypatch.setitem(discord_bot._runtime, "client", client)
    monkeypatch.setitem(discord_bot._runtime, "loop", _ImmediateLoop())
    monkeypatch.setitem(discord_bot._state, "connected", True)

    class _Future:
        def __init__(self, coro):
            self.coro = coro

        def result(self, timeout=None):
            try:
                self.coro.send(None)
            except StopIteration as stop:
                return stop.value
            raise AssertionError("coroutine awaited something real")

    monkeypatch.setattr(discord_bot.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: _Future(coro))
    if discord_module is not None:
        monkeypatch.setattr(discord_bot, "_try_import_discord",
                            lambda: (discord_module, None, None))


class _FakeDiscordModule:
    class Forbidden(Exception):
        pass

    class NotFound(Exception):
        pass


def test_send_dm_delivers_to_the_user(monkeypatch):
    user = _FakeUser()
    _patch_runtime(monkeypatch, _FakeClient(user=user), _FakeDiscordModule)
    ok, error = discord_bot.send_dm("123", "hello")
    assert (ok, error) == (True, "")
    assert user.sent == ["hello"]


def test_send_dm_refuses_when_the_bot_isnt_connected(monkeypatch):
    monkeypatch.setitem(discord_bot._runtime, "client", None)
    monkeypatch.setitem(discord_bot._runtime, "loop", None)
    ok, error = discord_bot.send_dm("123", "hello")
    assert ok is False
    assert "isn't connected" in error


def test_a_forbidden_dm_explains_discords_actual_rule(monkeypatch):
    """The failure an admin will really hit, and the one they'd never diagnose from a
    generic error: a bot may only DM someone who shares a server with it and allows
    DMs from server members."""
    _patch_runtime(monkeypatch, _FakeClient(raises=_FakeDiscordModule.Forbidden()),
                   _FakeDiscordModule)
    ok, error = discord_bot.send_dm("123", "hello")
    assert ok is False
    assert "shares a server" in error


def test_an_unknown_user_id_is_reported_as_such(monkeypatch):
    _patch_runtime(monkeypatch, _FakeClient(raises=_FakeDiscordModule.NotFound()),
                   _FakeDiscordModule)
    ok, error = discord_bot.send_dm("123", "hello")
    assert ok is False and "No Discord user" in error


def test_a_non_numeric_user_id_is_reported_not_raised(monkeypatch):
    _patch_runtime(monkeypatch, _FakeClient(), _FakeDiscordModule)
    ok, error = discord_bot.send_dm("not-an-id", "hello")
    assert ok is False and "isn't a valid Discord user ID" in error


def test_broadcast_reports_partial_delivery(monkeypatch):
    """"3 of 4 delivered, and why the fourth didn't" is far more useful than a single
    pass/fail for the whole batch."""
    monkeypatch.setattr(discord_bot, "send_dm",
                        lambda uid, text: (True, "") if uid == "1" else (False, "nope"))
    sent, failures = discord_bot.broadcast_dm("hi", user_ids=["1", "2"])
    assert sent == 1
    assert failures == [("2", "nope")]


def test_the_dm_list_is_default_closed(isolated_db):
    """The opposite of the bot's other three ID lists, deliberately. Those decide who
    may *ask* the bot for something; this decides who it messages unprompted, and empty
    must mean nobody."""
    assert discord_bot.dm_user_ids() == []
    db.set_setting("discordbot_dm_user_ids", "111, 222")
    assert discord_bot.dm_user_ids() == ["111", "222"]


def test_dm_ids_are_normalised_like_every_other_id_list():
    assert discord_bot.normalize_dm_user_ids("111\n222,  333 ") == "111, 222, 333"


# ---------------------------------------------------------------------------
# The approval check
# ---------------------------------------------------------------------------
def _seerr(monkeypatch, pending, total=None):
    monkeypatch.setattr(integrations, "fetch_seerr_pending",
                        lambda url, key, limit=50: (pending, total if total is not None else len(pending)))


def _request(rid, title="Some Film"):
    return {"id": rid, "title": title, "media_type": "movie",
            "requested_by": "Adam", "requested_at": "2026-08-21T10:00:00Z"}


@pytest.fixture
def seerr_configured(isolated_db):
    db.create_integration({"name": "Jellyseerr", "kind": "jellyseerr",
                            "base_url": "http://seerr.example", "api_key": "k", "enabled": 1})


def test_task_skips_without_a_seerr_integration(isolated_db):
    with pytest.raises(scheduler.TaskSkipped):
        seerr_alerts.run_approval_check()


def test_the_count_is_stored_for_the_admin_page(seerr_configured, monkeypatch):
    _seerr(monkeypatch, [_request(1), _request(2)], total=7)
    seerr_alerts.run_approval_check()
    assert seerr_alerts.pending_count() == 7
    assert seerr_alerts.last_checked_at() is not None


def test_a_failed_poll_leaves_the_previous_count_alone(seerr_configured, monkeypatch):
    """"Unknown" must not be rendered as "nothing to approve" - overwriting a real
    count with 0 would quietly tell the admin their queue was empty."""
    _seerr(monkeypatch, [_request(1)], total=3)
    seerr_alerts.run_approval_check()

    def boom(url, key, limit=50):
        raise integrations.requests.RequestException("down")

    monkeypatch.setattr(integrations, "fetch_seerr_pending", boom)
    with pytest.raises(RuntimeError):
        seerr_alerts.run_approval_check()
    assert seerr_alerts.pending_count() == 3


def test_dms_are_edge_triggered_not_repeated(seerr_configured, monkeypatch):
    """Fires once when a request appears, never again while it sits there waiting."""
    db.set_setting(seerr_alerts.DM_ENABLED_SETTING, "1")
    db.set_setting("discordbot_dm_user_ids", "111")
    sent = []
    monkeypatch.setattr(discord_bot, "broadcast_dm",
                        lambda text, user_ids=None: (sent.append(text), (1, []))[1])
    _seerr(monkeypatch, [_request(1)])
    seerr_alerts.run_approval_check()
    seerr_alerts.run_approval_check()
    seerr_alerts.run_approval_check()
    assert len(sent) == 1


def test_a_new_request_alongside_an_old_one_only_announces_the_new(seerr_configured, monkeypatch):
    db.set_setting(seerr_alerts.DM_ENABLED_SETTING, "1")
    db.set_setting("discordbot_dm_user_ids", "111")
    sent = []
    monkeypatch.setattr(discord_bot, "broadcast_dm",
                        lambda text, user_ids=None: (sent.append(text), (1, []))[1])
    _seerr(monkeypatch, [_request(1, "First")])
    seerr_alerts.run_approval_check()
    _seerr(monkeypatch, [_request(1, "First"), _request(2, "Second")])
    seerr_alerts.run_approval_check()
    assert len(sent) == 2
    assert "Second" in sent[1] and "First" not in sent[1]


def test_a_request_nobody_was_told_about_stays_new(seerr_configured, monkeypatch):
    """A disconnected bot must mean a delayed alert, not a swallowed one - so a request
    is only marked announced once a DM actually reached somebody."""
    db.set_setting(seerr_alerts.DM_ENABLED_SETTING, "1")
    db.set_setting("discordbot_dm_user_ids", "111")
    monkeypatch.setattr(discord_bot, "broadcast_dm",
                        lambda text, user_ids=None: (0, [("111", "not connected")]))
    _seerr(monkeypatch, [_request(1)])
    message = seerr_alerts.run_approval_check()
    assert "failed" in message
    assert seerr_alerts._notified_ids() == []

    sent = []
    monkeypatch.setattr(discord_bot, "broadcast_dm",
                        lambda text, user_ids=None: (sent.append(text), (1, []))[1])
    seerr_alerts.run_approval_check()
    assert len(sent) == 1


def test_turning_dms_on_later_doesnt_dump_the_backlog(seerr_configured, monkeypatch):
    """While DMs are off, what's pending is still remembered - so switching them on
    announces what arrives *next* rather than emptying the whole existing queue into
    somebody's inbox at once."""
    sent = []
    monkeypatch.setattr(discord_bot, "broadcast_dm",
                        lambda text, user_ids=None: (sent.append(text), (1, []))[1])
    _seerr(monkeypatch, [_request(1), _request(2), _request(3)])
    seerr_alerts.run_approval_check()          # DMs off
    assert sent == []

    db.set_setting(seerr_alerts.DM_ENABLED_SETTING, "1")
    db.set_setting("discordbot_dm_user_ids", "111")
    seerr_alerts.run_approval_check()
    assert sent == []                          # the backlog stays quiet

    _seerr(monkeypatch, [_request(1), _request(2), _request(3), _request(4, "Brand New")])
    seerr_alerts.run_approval_check()
    assert len(sent) == 1 and "Brand New" in sent[0]


def test_the_notified_list_survives_a_restart(seerr_configured, monkeypatch):
    """Persisted in the settings table, not a module global: a restart while requests
    are still pending must not re-announce every one of them."""
    db.set_setting(seerr_alerts.DM_ENABLED_SETTING, "1")
    db.set_setting("discordbot_dm_user_ids", "111")
    monkeypatch.setattr(discord_bot, "broadcast_dm", lambda text, user_ids=None: (1, []))
    _seerr(monkeypatch, [_request(1)])
    seerr_alerts.run_approval_check()
    # A fresh read straight from the database - nothing in memory is consulted.
    assert json.loads(db.get_setting(seerr_alerts.NOTIFIED_SETTING)) == [1]


def test_the_remembered_id_list_is_bounded(seerr_configured, monkeypatch):
    """It lives in one settings row, so it can't be allowed to grow forever."""
    monkeypatch.setattr(seerr_alerts, "MAX_REMEMBERED_IDS", 5)
    seerr_alerts._remember_ids(set(range(100)))
    assert len(seerr_alerts._notified_ids()) == 5
    # The oldest are the safe ones to forget - ids increase over time.
    assert seerr_alerts._notified_ids() == [95, 96, 97, 98, 99]


def test_a_corrupted_notified_list_starts_over_rather_than_crashing(seerr_configured):
    db.set_setting(seerr_alerts.NOTIFIED_SETTING, "{not json")
    assert seerr_alerts._notified_ids() == []


def test_the_alert_text_names_the_title_and_requester(monkeypatch):
    text = seerr_alerts.format_alert(_request(1, "Dune: Part Three"))
    assert "Dune: Part Three" in text and "Adam" in text


# ---------------------------------------------------------------------------
# Issue tracking (part of the Seerr event lifecycle - see track_issue_updates())
# ---------------------------------------------------------------------------
def _issue(iid, status="open", comment_count=0, title="Some Film", created_by_id="9"):
    return {"id": iid, "title": title, "poster_path": "", "issue_type": "video",
            "status": status, "created_by_id": created_by_id, "created_by_name": "Pat",
            "comment_count": comment_count}


def _issues(monkeypatch, issues):
    monkeypatch.setattr(integrations, "fetch_seerr_issues", lambda url, key, limit=50: issues)


def test_a_new_issue_alerts_the_admin_but_not_the_reporter(isolated_db, monkeypatch):
    """A brand-new issue is already implicitly "reported by" its own creator - only an
    update is news to them specifically. The admin needs to hear about both."""
    db.set_user_preferences("u1", seerr_user_id="9", notify_discord_id="123")
    _issues(monkeypatch, [_issue(1)])
    count, alerts = seerr_alerts.track_issue_updates({"base_url": "http://s", "api_key": "k"})
    assert count == 1
    assert "New issue" in alerts[0]
    assert db.pending_notifications() == []


def test_an_issue_status_change_notifies_both(isolated_db, monkeypatch):
    db.set_setting("user_notifications_enabled", "1")
    db.set_user_preferences("u1", seerr_user_id="9", notify_discord_id="123")
    integration = {"base_url": "http://s", "api_key": "k"}
    _issues(monkeypatch, [_issue(1, status="open")])
    seerr_alerts.track_issue_updates(integration)
    _issues(monkeypatch, [_issue(1, status="resolved")])
    count, alerts = seerr_alerts.track_issue_updates(integration)
    assert count == 1
    assert "Issue update" in alerts[0]
    queued = db.pending_notifications()
    assert len(queued) == 1
    assert queued[0]["user_id"] == "u1"
    assert queued[0]["event"] == "seerr_event"


def test_a_new_comment_counts_as_an_update(isolated_db, monkeypatch):
    integration = {"base_url": "http://s", "api_key": "k"}
    _issues(monkeypatch, [_issue(1, comment_count=1)])
    seerr_alerts.track_issue_updates(integration)
    _issues(monkeypatch, [_issue(1, comment_count=2)])
    count, _alerts = seerr_alerts.track_issue_updates(integration)
    assert count == 1


def test_nothing_changed_produces_no_alert(isolated_db, monkeypatch):
    integration = {"base_url": "http://s", "api_key": "k"}
    _issues(monkeypatch, [_issue(1)])
    seerr_alerts.track_issue_updates(integration)
    count, alerts = seerr_alerts.track_issue_updates(integration)
    assert count == 0
    assert alerts == []


def test_an_unlinked_issue_reporter_only_alerts_the_admin(isolated_db, monkeypatch):
    """No Jellyfin<->Seerr link means no per-user notification, same rule as request
    progress - but the admin-directed alert doesn't depend on that link at all."""
    integration = {"base_url": "http://s", "api_key": "k"}
    _issues(monkeypatch, [_issue(1, status="open", created_by_id="")])
    seerr_alerts.track_issue_updates(integration)
    _issues(monkeypatch, [_issue(1, status="resolved", created_by_id="")])
    count, alerts = seerr_alerts.track_issue_updates(integration)
    assert count == 1
    assert db.pending_notifications() == []


def test_issue_states_survive_a_restart(isolated_db, monkeypatch):
    _issues(monkeypatch, [_issue(1, status="open", comment_count=2)])
    seerr_alerts.track_issue_updates({"base_url": "http://s", "api_key": "k"})
    stored = json.loads(db.get_setting(seerr_alerts.ISSUE_STATES_SETTING))
    assert stored == {"1": {"status": "open", "comment_count": 2}}


def test_a_failed_issue_poll_does_not_crash_the_approval_check(seerr_configured, monkeypatch):
    """The approval count is this task's primary job - an issue-tracking failure must
    be logged and skipped, not let the whole scheduled run fail."""
    def boom(url, key, limit=50):
        raise integrations.requests.RequestException("down")

    monkeypatch.setattr(integrations, "fetch_seerr_issues", boom)
    _seerr(monkeypatch, [_request(1)], total=1)
    message = seerr_alerts.run_approval_check()
    assert "1 awaiting approval" in message


def test_issue_alerts_are_delivered_alongside_approval_alerts(seerr_configured, monkeypatch):
    db.set_setting(seerr_alerts.DM_ENABLED_SETTING, "1")
    db.set_setting("discordbot_dm_user_ids", "111")
    sent = []
    monkeypatch.setattr(discord_bot, "broadcast_dm",
                        lambda text, user_ids=None: (sent.append(text), (1, []))[1])
    _seerr(monkeypatch, [])
    _issues(monkeypatch, [_issue(1, title="A Reported Film")])
    message = seerr_alerts.run_approval_check()
    assert any("A Reported Film" in t for t in sent)
    assert "1 issue update" in message
