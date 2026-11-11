"""TCP-port health checks: the check type is per service, HTTP stays the default."""
import socket

import pytest

import db
import health as health_module


@pytest.fixture
def listening_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


@pytest.fixture
def closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _login(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})


def test_tcp_check_open_port_is_operational(listening_port):
    status, elapsed_ms = health_module._run_single_check(f"127.0.0.1:{listening_port}", None, "tcp")
    assert status == "operational"
    assert elapsed_ms is not None


def test_tcp_check_closed_port_is_down(closed_port):
    assert health_module._run_single_check(f"127.0.0.1:{closed_port}", None, "tcp") == ("down", None)


def test_tcp_check_slow_threshold(listening_port, monkeypatch):
    ticks = iter([0.0, 0.5])
    monkeypatch.setattr(health_module.time, "perf_counter", lambda: next(ticks))
    status, elapsed_ms = health_module._run_single_check(f"127.0.0.1:{listening_port}", 100, "tcp")
    assert (status, elapsed_ms) == ("slow", 500)


@pytest.mark.parametrize("target", ["", "localhost", "host:", "host:abc", "host:0", "host:70000", ":80"])
def test_tcp_check_target_that_is_not_host_port_is_down(target):
    assert health_module._run_single_check(target, None, "tcp") == ("down", None)


def test_tcp_target_parsing_tolerates_scheme_path_and_ipv6():
    assert health_module._split_host_port("tcp://nas.lan:445/") == ("nas.lan", 445)
    assert health_module._split_host_port("[::1]:80") == ("::1", 80)


def test_tcp_check_timeout_is_five_seconds(monkeypatch):
    seen = {}

    def fake_connect(endpoint, timeout):
        seen["timeout"] = timeout
        raise socket.timeout()

    monkeypatch.setattr(health_module.socket, "create_connection", fake_connect)
    assert health_module._run_single_check("nas:445", None, "tcp")[0] == "down"
    assert seen["timeout"] == 5


def test_http_stays_the_default_check(monkeypatch):
    calls = []

    class Resp:
        status_code = 200

        def close(self):
            pass

    monkeypatch.setattr(health_module.requests, "get", lambda url, **kw: calls.append(url) or Resp())
    assert health_module._run_single_check("http://x", None)[0] == "operational"
    assert calls == ["http://x"]


def test_tcp_service_retries_like_http(monkeypatch):
    results = iter([("down", None), ("operational", 3)])
    seen = []

    def fake_check(target, threshold, check_type="http"):
        seen.append((target, check_type))
        return next(results)

    monkeypatch.setattr(health_module, "_run_single_check", fake_check)
    monkeypatch.setattr(health_module.time, "sleep", lambda s: None)
    service = {"check_url": "nas:445", "check_type": "tcp", "slow_threshold_ms": None,
               "retry_count": 1, "retry_interval_seconds": 1}
    assert health_module._check_service_status(service) == ("operational", 3)
    assert seen == [("nas:445", "tcp")] * 2


def test_tcp_service_without_check_type_key_uses_http(monkeypatch):
    seen = []
    monkeypatch.setattr(health_module, "_run_single_check",
                        lambda url, thr, check_type="http": seen.append(check_type) or ("operational", 1))
    health_module._check_service_status({"check_url": "http://x", "slow_threshold_ms": None})
    assert seen == ["http"]


def test_tcp_down_opens_an_incident_and_is_recorded(client, closed_port):
    sid = db.create_service({"name": "NAS", "url": "http://nas", "auto_check": 1,
                             "check_url": f"127.0.0.1:{closed_port}", "check_type": "tcp"})
    service = db.get_service(sid)
    assert service["check_type"] == "tcp"
    health_module._check_and_record_service(service, {})
    after = db.get_service(sid)
    assert after["status"] == "down"


def test_service_check_type_defaults_to_http_and_rejects_junk(client):
    sid = db.create_service({"name": "A", "url": "http://a"})
    assert db.get_service(sid)["check_type"] == "http"
    sid = db.create_service({"name": "B", "url": "http://b", "check_type": "icmp"})
    assert db.get_service(sid)["check_type"] == "http"


def test_main_form_saves_and_shows_check_type(client):
    _login(client)
    client.post("/admin/services/new", data={
        "name": "NAS", "url": "http://nas", "status": "operational", "check_url": "nas:445",
        "check_type": "tcp", "auto_check": "on"})
    service = next(s for s in db.list_services() if s["name"] == "NAS")
    assert service["check_type"] == "tcp"
    page = client.get(f"/admin/services/{service['id']}/edit").get_data(as_text=True)
    assert '<option value="tcp" selected>' in page
    client.post(f"/admin/services/{service['id']}/edit", data={
        "name": "NAS", "url": "http://nas", "status": "operational", "check_type": "http"})
    assert db.get_service(service["id"])["check_type"] == "http"


def test_wizard_has_and_saves_check_type(client):
    _login(client)
    assert 'name="check_type"' in client.get("/admin/new/combined").get_data(as_text=True)
    client.post("/admin/new/combined", data={
        "name": "NAS", "url": "http://nas", "kind": "arr", "api_key": "k",
        "check_url": "nas:445", "check_type": "tcp", "auto_check": "on"})
    assert next(s for s in db.list_services() if s["name"] == "NAS")["check_type"] == "tcp"
