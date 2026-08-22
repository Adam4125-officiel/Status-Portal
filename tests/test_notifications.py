import config
import notifications


def _no_email(monkeypatch):
    """Every pre-existing test here asserts on requests.post call counts, which say
    nothing about email - but a developer machine with SMTP set in its .env would still
    make notify() try to send one. Pinned off explicitly so these tests assert the same
    thing everywhere."""
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_FROM", "")
    monkeypatch.setattr(config, "SMTP_TO", "")


def test_notify_does_nothing_when_unconfigured(monkeypatch):
    _no_email(monkeypatch)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NTFY_URL", "")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda *a, **k: calls.append((a, k)))
    notifications.notify("Title", "Message")
    assert calls == []


def test_notify_sends_discord_payload(monkeypatch):
    _no_email(monkeypatch)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "NTFY_URL", "")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda *a, **k: calls.append((a, k)))
    notifications.notify("Incident opened", "Jellyfin is unreachable.")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "https://discord.example/webhook"
    assert "Incident opened" in kwargs["json"]["content"]
    assert "Jellyfin is unreachable." in kwargs["json"]["content"]


def test_notify_sends_ntfy_payload(monkeypatch):
    _no_email(monkeypatch)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NTFY_URL", "https://ntfy.sh/my-topic")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda *a, **k: calls.append((a, k)))
    notifications.notify("Incident resolved", "Jellyfin has recovered.")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "https://ntfy.sh/my-topic"
    assert b"Incident resolved" in kwargs["data"]
    assert b"Jellyfin has recovered." in kwargs["data"]


def test_notify_swallows_exceptions(monkeypatch):
    _no_email(monkeypatch)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "NTFY_URL", "https://ntfy.sh/my-topic")

    def _boom(*a, **k):
        raise ConnectionError("nope")

    monkeypatch.setattr(notifications.requests, "post", _boom)
    notifications.notify("Title", "Message")  # must not raise


# ---------------------------------------------------------------------------
# Email - the third channel
# ---------------------------------------------------------------------------
import email as email_mod  # noqa: E402
import socket           # noqa: E402
import threading        # noqa: E402

import pytest           # noqa: E402


def _configure_email(monkeypatch, **overrides):
    values = {"SMTP_HOST": "smtp.example", "SMTP_PORT": 587, "SMTP_USERNAME": "",
              "SMTP_PASSWORD": "", "SMTP_FROM": "portal@example.com",
              "SMTP_TO": "admin@example.com", "SMTP_SECURITY": "starttls",
              "SMTP_TIMEOUT_SECONDS": 10}
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(config, key, value)


class _FakeSMTP:
    """Records the conversation instead of having one."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.login_args = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.instances = []


def test_email_is_not_configured_until_host_from_and_to_are_all_set(monkeypatch):
    """A half-filled block counts as "not set up", so the admin page says so instead of
    the log filling with the same error on every service blip."""
    _configure_email(monkeypatch, SMTP_TO="")
    assert notifications.email_configured() is False
    _configure_email(monkeypatch, SMTP_FROM="")
    assert notifications.email_configured() is False
    _configure_email(monkeypatch)
    assert notifications.email_configured() is True


def test_recipients_tolerate_stray_commas_and_spaces(monkeypatch):
    _configure_email(monkeypatch, SMTP_TO=" a@example.com , b@example.com ,")
    assert notifications.email_recipients() == ["a@example.com", "b@example.com"]


def test_notify_sends_email_alongside_the_other_channels(monkeypatch):
    _configure_email(monkeypatch)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NTFY_URL", "")
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.notify("Incident opened", "Jellyfin is unreachable.")
    assert len(_FakeSMTP.instances) == 1
    sent = _FakeSMTP.instances[0].sent[0]
    assert sent["Subject"] == "Incident opened"
    assert sent["To"] == "admin@example.com"


def test_starttls_is_used_for_the_default_security_mode(monkeypatch):
    _configure_email(monkeypatch)
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.send_email("s", "m")
    assert _FakeSMTP.instances[0].started_tls is True


def test_an_unrecognised_security_mode_upgrades_rather_than_downgrades(monkeypatch):
    """Failing to connect is a far better outcome than quietly sending credentials in
    the clear because someone typo'd the setting."""
    _configure_email(monkeypatch, SMTP_SECURITY="startls")
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.send_email("s", "m")
    assert _FakeSMTP.instances[0].started_tls is True


def test_security_none_sends_without_tls(monkeypatch):
    _configure_email(monkeypatch, SMTP_SECURITY="none")
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.send_email("s", "m")
    assert _FakeSMTP.instances[0].started_tls is False


def test_ssl_mode_uses_an_implicitly_encrypted_connection(monkeypatch):
    _configure_email(monkeypatch, SMTP_SECURITY="ssl", SMTP_PORT=465)
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FakeSMTP)
    notifications.send_email("s", "m")
    assert _FakeSMTP.instances[0].port == 465


def test_login_is_skipped_when_no_username_is_configured(monkeypatch):
    """A relay on the same machine typically wants no authentication at all, and
    calling login() against one is an error rather than a no-op."""
    _configure_email(monkeypatch, SMTP_USERNAME="")
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.send_email("s", "m")
    assert _FakeSMTP.instances[0].login_args is None


def test_login_happens_when_a_username_is_configured(monkeypatch):
    _configure_email(monkeypatch, SMTP_USERNAME="me", SMTP_PASSWORD="secret")
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.send_email("s", "m")
    assert _FakeSMTP.instances[0].login_args == ("me", "secret")


def test_a_failing_smtp_server_never_raises(monkeypatch):
    """Same contract as every other channel: a notification failing must not break the
    health check or the admin action that triggered it."""
    _configure_email(monkeypatch)

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(notifications.smtplib, "SMTP", boom)
    assert notifications.send_email("s", "m") is False
    notifications.notify("Title", "Message")  # must not raise either


def test_recipients_can_be_overridden_for_per_user_mail(monkeypatch):
    """The parameter exists so per-user notifications can reuse this without going
    anywhere near PORTAL_SMTP_TO, which is the admin alert list."""
    _configure_email(monkeypatch)
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.send_email("s", "m", recipients=["someone@else.example"])
    assert _FakeSMTP.instances[0].sent[0]["To"] == "someone@else.example"


# ---------------------------------------------------------------------------
# The message itself
# ---------------------------------------------------------------------------
def test_the_email_offers_plain_text_and_html(monkeypatch):
    _configure_email(monkeypatch)
    message = notifications.build_email("Incident opened", "Jellyfin is down.",
                                         ["admin@example.com"])
    types = [part.get_content_type() for part in message.walk()]
    assert "text/plain" in types and "text/html" in types


def test_html_is_the_preferred_alternative(monkeypatch):
    """In multipart/alternative the *last* part is the preferred one, so plain text has
    to be set first for this to be the right way round."""
    _configure_email(monkeypatch)
    message = notifications.build_email("s", "m", ["admin@example.com"])
    parts = [p for p in message.walk() if p.get_content_type() in ("text/plain", "text/html")]
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]


def test_the_html_body_escapes_its_inputs(monkeypatch):
    """A service name is admin-controlled, but an email body is still never a place to
    interpolate an unescaped string - and integration-derived names reach these
    messages too."""
    _configure_email(monkeypatch)
    message = notifications.build_email("<script>alert(1)</script>",
                                         "<img src=x onerror=alert(2)>",
                                         ["admin@example.com"])
    html_part = [p for p in message.walk() if p.get_content_type() == "text/html"][0]
    body = html_part.get_content()
    assert "<script>" not in body and "<img" not in body
    assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# Against a real SMTP conversation, not a fake object
# ---------------------------------------------------------------------------
class _TinySMTPServer(threading.Thread):
    """Speaks just enough SMTP to accept one message, so the actual smtplib
    conversation is exercised rather than mocked away. No TLS - the point here is the
    protocol flow and the bytes that arrive, not the crypto."""
    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = None

    def run(self):
        conn, _ = self.sock.accept()
        with conn, conn.makefile("rwb") as stream:
            def reply(text):
                conn.sendall(text.encode() + b"\r\n")
            reply("220 tiny ESMTP")
            data_mode, body = False, []
            while True:
                line = stream.readline()
                if not line:
                    break
                if data_mode:
                    if line.strip() == b".":
                        data_mode = False
                        self.received = b"".join(body).decode("utf-8", "replace")
                        reply("250 OK")
                    else:
                        body.append(line)
                    continue
                command = line.decode("utf-8", "replace").strip().upper()
                if command.startswith(("EHLO", "HELO")):
                    reply("250-tiny")
                    reply("250 HELP")
                elif command.startswith(("MAIL", "RCPT")):
                    reply("250 OK")
                elif command.startswith("DATA"):
                    reply("354 send it")
                    data_mode = True
                elif command.startswith("QUIT"):
                    reply("221 bye")
                    break
                else:
                    reply("250 OK")
        self.sock.close()


def test_a_real_smtp_conversation_delivers_the_message(monkeypatch):
    """The stand-in-server test. Everything above mocks smtplib away; this one actually
    connects, talks SMTP and inspects the bytes that arrived - which is what would catch
    a malformed header or a body that never got encoded."""
    server = _TinySMTPServer()
    server.start()
    _configure_email(monkeypatch, SMTP_HOST="127.0.0.1", SMTP_PORT=server.port,
                     SMTP_SECURITY="none", SMTP_TO="admin@example.com")
    assert notifications.send_email("Incident opened", "Jellyfin is unreachable.") is True
    server.join(timeout=5)

    assert server.received is not None
    parsed = email_mod.message_from_string(server.received)
    assert parsed["Subject"] == "Incident opened"
    assert parsed["From"] == "portal@example.com"
    assert parsed["To"] == "admin@example.com"
    bodies = [p.get_payload(decode=True).decode() for p in parsed.walk()
              if p.get_content_type() in ("text/plain", "text/html")]
    assert any("Jellyfin is unreachable." in b for b in bodies)
    assert any("<h2" in b for b in bodies)


# ---------------------------------------------------------------------------
# Recipients are a database setting, not an env var
# ---------------------------------------------------------------------------
import db as db_module  # noqa: E402


def test_the_stored_recipient_list_wins_over_the_env_var(isolated_db, monkeypatch):
    _configure_email(monkeypatch, SMTP_TO="from-env@example.invalid")
    db_module.set_setting(notifications.RECIPIENTS_SETTING, "from-db@example.invalid")
    assert notifications.email_recipients() == ["from-db@example.invalid"]


def test_the_env_var_still_works_for_an_install_that_predates_this(isolated_db, monkeypatch):
    """Moving a setting must not silently switch off notifications for someone who
    configured it the old way and hasn't opened the admin page since."""
    _configure_email(monkeypatch, SMTP_TO="legacy@example.invalid")
    assert notifications.email_recipients() == ["legacy@example.invalid"]


def test_an_unreadable_database_never_breaks_notify(monkeypatch):
    """notify() runs on the background health-check thread and is documented as never
    raising. Reading a setting is a new way for that to happen, so it has to fall back
    rather than propagate."""
    _configure_email(monkeypatch, SMTP_TO="fallback@example.invalid")

    def boom(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(notifications.db, "get_setting", boom)
    assert notifications.email_recipients() == ["fallback@example.invalid"]
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)
    notifications.notify("Title", "Message")  # must not raise


@pytest.mark.parametrize("raw, expected", [
    ("a@x.invalid,b@x.invalid", "a@x.invalid, b@x.invalid"),
    ("a@x.invalid\nb@x.invalid", "a@x.invalid, b@x.invalid"),
    ("  a@x.invalid ,, ", "a@x.invalid"),
])
def test_recipient_input_is_normalised_like_the_discord_id_lists(raw, expected):
    assert notifications.normalize_recipients(raw) == expected


def test_the_admin_page_saves_recipients(client, isolated_db):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/notifications",
                 data={"recipients": "one@example.invalid\ntwo@example.invalid"},
                 follow_redirects=True)
    assert db_module.get_setting(notifications.RECIPIENTS_SETTING) == \
        "one@example.invalid, two@example.invalid"
