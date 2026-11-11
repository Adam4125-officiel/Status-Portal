"""Daily TLS certificate expiry alert: edge-triggered, state in the database."""
import shutil
import socket
import ssl
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

import db
import health as health_module


def _expiry_in(days):
    return datetime.now(timezone.utc) + timedelta(days=days, hours=1)


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(health_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    return calls


def _https_service(url="https://example.test", **extra):
    return db.create_service({"name": "Wiki", "url": url, "auto_check": 1, "check_url": url, **extra})


def test_warns_once_per_crossing_and_again_after_renewal(isolated_db, notified, monkeypatch):
    sid = _https_service()
    days = {"left": 5}
    monkeypatch.setattr(health_module, "_read_cert_expiry", lambda host, port: _expiry_in(days["left"]))

    health_module._check_tls_certificates()
    health_module._check_tls_certificates()
    assert [c[0] for c in notified] == ["TLS certificate expiring"]
    assert "Wiki" in notified[0][1]
    assert db.get_tls_expiry(sid) != ""

    days["left"] = 60  # renewed: silent, state reset
    health_module._check_tls_certificates()
    assert len(notified) == 1 and db.get_tls_alert_state(sid) is False

    days["left"] = 3  # runs low again: a new crossing
    health_module._check_tls_certificates()
    assert len(notified) == 2


def test_warning_survives_a_restart(isolated_db, notified, monkeypatch):
    sid = _https_service()
    db.set_tls_alert_state(sid, True)
    monkeypatch.setattr(health_module, "_read_cert_expiry", lambda host, port: _expiry_in(2))
    health_module._check_tls_certificates()
    assert notified == []


def test_threshold_setting_and_default(isolated_db, notified, monkeypatch):
    _https_service()
    monkeypatch.setattr(health_module, "_read_cert_expiry", lambda host, port: _expiry_in(20))
    assert health_module._tls_alert_days() == 14
    health_module._check_tls_certificates()
    assert notified == []
    db.set_setting("tls_expiry_alert_days", "30")
    health_module._check_tls_certificates()
    assert len(notified) == 1


def test_unreadable_certificate_changes_nothing(isolated_db, notified, monkeypatch):
    sid = _https_service()
    db.set_tls_alert_state(sid, True)
    monkeypatch.setattr(health_module, "_read_cert_expiry", lambda host, port: None)
    health_module._check_tls_certificates()
    assert notified == [] and db.get_tls_alert_state(sid) is True


def test_only_auto_checked_https_http_services_are_read(isolated_db, monkeypatch):
    _https_service("http://plain.test")
    _https_service("https://tcp.test:443", check_type="tcp")
    _https_service("https://off.test", auto_check=0)
    seen = []
    monkeypatch.setattr(health_module, "_read_cert_expiry", lambda host, port: seen.append((host, port)))
    _https_service("https://wiki.test:8443/path")
    health_module._check_tls_certificates()
    assert seen == [("wiki.test", 8443)]


def test_task_is_skipped_without_https_services(isolated_db):
    import scheduler
    with pytest.raises(scheduler.TaskSkipped):
        health_module._tls_certificates_task()


def test_deleting_a_service_drops_its_state(isolated_db):
    sid = _https_service()
    db.set_tls_alert_state(sid, True)
    db.set_tls_expiry(sid, "2030-01-01")
    db.delete_service(sid)
    assert db.get_setting(f"tls_alert_state:{sid}") is None
    assert db.get_tls_expiry(sid) == ""


def test_settings_form_saves_the_threshold_and_services_page_shows_expiry(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = _https_service()
    db.set_tls_expiry(sid, "2031-02-03")
    client.post("/admin/settings/monitoring", data={"tls_expiry_alert_days": "21"})
    assert db.get_setting("tls_expiry_alert_days") == "21"
    assert "2031-02-03" in client.get("/admin/services").data.decode()
    assert "2031-02-03" not in client.get("/").data.decode()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_reads_a_real_self_signed_certificate(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "5", "-subj", "/CN=localhost"],
                   check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]

    def serve():
        for _ in range(3):
            try:
                conn, _addr = server.accept()
                try:
                    ctx.wrap_socket(conn, server_side=True).close()
                except (ssl.SSLError, OSError):
                    conn.close()
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        expires = health_module._read_cert_expiry("127.0.0.1", port)
    finally:
        server.close()
        thread.join(timeout=5)
    assert expires is not None
    assert 4 <= (expires - datetime.now(timezone.utc)).days <= 5


def test_unreachable_host_reads_as_none():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert health_module._read_cert_expiry("127.0.0.1", port) is None
