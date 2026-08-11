from unittest.mock import Mock, patch

import requests

import integrations


def test_fetch_arr_status_unreachable():
    result = integrations.fetch_arr_status("http://localhost:1", "fake-key")
    assert result["reachable"] is False
    assert result["error"]


def test_fetch_arr_status_parses_health_issues():
    fake_health = [
        {"type": "warning", "message": "Indexers unavailable"},
        {"type": "error", "message": "low disk space"},
    ]
    mock_resp = Mock(status_code=200)
    mock_resp.raise_for_status = Mock()
    mock_resp.json = Mock(return_value=fake_health)
    with patch("requests.get", return_value=mock_resp):
        result = integrations.fetch_arr_status("http://sonarr:8989", "key")
    assert result["reachable"] is True
    assert result["issues"] == [
        {"level": "warning", "message": "Indexers unavailable"},
        {"level": "error", "message": "low disk space"},
    ]


def test_fetch_jellyfin_status_parses_info_and_log():
    info_resp = Mock(status_code=200)
    info_resp.raise_for_status = Mock()
    info_resp.json = Mock(return_value={"Version": "10.9.0"})
    log_resp = Mock(ok=True)
    log_resp.json = Mock(return_value={"Items": [{"Severity": "Warning", "Name": "Playback failed"}]})
    with patch("requests.get", side_effect=[info_resp, log_resp]):
        result = integrations.fetch_jellyfin_status("http://jellyfin:8096", "key")
    assert result["reachable"] is True
    assert result["version"] == "10.9.0"
    assert result["issues"] == [{"level": "warning", "message": "Playback failed"}]


def test_fetch_jellyseerr_status_parses_status_and_log():
    status_resp = Mock(status_code=200)
    status_resp.raise_for_status = Mock()
    status_resp.json = Mock(return_value={"version": "1.9.2"})
    log_resp = Mock(ok=True)
    log_resp.json = Mock(return_value={"results": [{"level": "warn", "message": "Radarr sync failed"}]})
    with patch("requests.get", side_effect=[status_resp, log_resp]):
        result = integrations.fetch_jellyseerr_status("http://jellyseerr:5055", "key")
    assert result["reachable"] is True
    assert result["version"] == "1.9.2"
    assert result["issues"] == [{"level": "warning", "message": "Radarr sync failed"}]


def test_fetch_integration_status_dispatch():
    assert integrations.fetch_integration_status(
        {"kind": "arr", "base_url": "http://localhost:1", "api_key": "x"}
    )["reachable"] is False

    result = integrations.fetch_integration_status({"kind": "unknown", "base_url": "x", "api_key": "y"})
    assert "Unknown integration kind" in result["error"]


def test_fetch_bazarr_status_unreachable():
    result = integrations.fetch_bazarr_status("http://localhost:1", "fake-key")
    assert result["reachable"] is False
    assert result["error"]


def test_fetch_bazarr_status_parses_version_and_health():
    status_resp = Mock(status_code=200)
    status_resp.raise_for_status = Mock()
    status_resp.json = Mock(return_value={"data": {"bazarr_version": "1.4.3"}})
    health_resp = Mock(ok=True)
    health_resp.json = Mock(return_value=[{"type": "warning", "text": "Sonarr sync failed"}])
    with patch("requests.get", side_effect=[status_resp, health_resp]) as mock_get:
        result = integrations.fetch_bazarr_status("http://bazarr:6767", "key")
    assert result["reachable"] is True
    assert result["version"] == "1.4.3"
    assert result["issues"] == [{"level": "warning", "message": "Sonarr sync failed"}]
    # Bazarr expects its key as a query param, not a header.
    assert mock_get.call_args_list[0].kwargs["params"] == {"apikey": "key"}


def test_fetch_bazarr_status_health_failure_does_not_fail_whole_check():
    status_resp = Mock(status_code=200)
    status_resp.raise_for_status = Mock()
    status_resp.json = Mock(return_value={"data": {"bazarr_version": "1.4.3"}})
    with patch("requests.get", side_effect=[status_resp, requests.RequestException("boom")]):
        result = integrations.fetch_bazarr_status("http://bazarr:6767", "key")
    assert result["reachable"] is True
    assert result["issues"] == []


def test_fetch_tdarr_status_unreachable():
    result = integrations.fetch_tdarr_status("http://localhost:1", "")
    assert result["reachable"] is False
    assert result["error"]


def test_fetch_tdarr_status_parses_version_and_flags_bad_status():
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value={"status": "bad", "version": "2.00.18"})
    with patch("requests.get", return_value=resp):
        result = integrations.fetch_tdarr_status("http://tdarr:8265", "")
    assert result["reachable"] is True
    assert result["version"] == "2.00.18"
    assert result["issues"] == [{"level": "warning", "message": "Server status: bad"}]


def test_fetch_tdarr_status_good_has_no_issues():
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value={"status": "good", "version": "2.00.18"})
    with patch("requests.get", return_value=resp):
        result = integrations.fetch_tdarr_status("http://tdarr:8265", "")
    assert result["issues"] == []


def test_fetch_byparr_status_unreachable():
    result = integrations.fetch_byparr_status("http://localhost:1", "")
    assert result["reachable"] is False
    assert result["error"]


def test_fetch_byparr_status_healthy():
    resp = Mock(status_code=200, ok=True)
    with patch("requests.get", return_value=resp):
        result = integrations.fetch_byparr_status("http://byparr:8191", "")
    assert result == {"reachable": True, "version": None, "issues": [], "error": None}


def test_fetch_byparr_status_challenge_solve_failure():
    resp = Mock(status_code=500, ok=False)
    with patch("requests.get", return_value=resp):
        result = integrations.fetch_byparr_status("http://byparr:8191", "")
    assert result["reachable"] is False
    assert "challenge" in result["error"]


def test_fetch_byparr_status_uses_its_own_longer_configurable_timeout(monkeypatch):
    """Regression test: Byparr's /health makes it actually solve a real Cloudflare
    challenge before responding, which routinely exceeds the 5s TIMEOUT every other
    fetcher uses for a plain REST call - a real instance was confirmed timing out
    against that shared value. Byparr must use its own, longer, configurable
    timeout instead, not the shared constant."""
    import config
    monkeypatch.setattr(config, "BYPARR_TIMEOUT_SECONDS", 45)
    resp = Mock(status_code=200, ok=True)
    with patch("requests.get", return_value=resp) as mock_get:
        integrations.fetch_byparr_status("http://byparr:8191", "")
    assert mock_get.call_args.kwargs["timeout"] == 45
    assert mock_get.call_args.kwargs["timeout"] != integrations.TIMEOUT


def test_fetch_jellyfin_sessions_counts_transcodes_only():
    fake_sessions = [
        {"PlayState": {"PlayMethod": "Transcode"}},
        {"PlayState": {"PlayMethod": "DirectPlay"}},
        {"PlayState": {"PlayMethod": "Transcode"}},
        {},  # no PlayState at all - shouldn't crash
    ]
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=fake_sessions)
    with patch("requests.get", return_value=resp):
        assert integrations.fetch_jellyfin_sessions("http://jellyfin:8096", "key") == 2


def test_fetch_jellyfin_sessions_degrades_to_zero_on_failure():
    assert integrations.fetch_jellyfin_sessions("http://localhost:1", "key") == 0


def test_fetch_jellyfin_running_tasks_filters_running_state():
    fake_tasks = [
        {"Name": "Trickplay Image Extraction", "State": "Running"},
        {"Name": "Scan Media Library", "State": "Idle"},
    ]
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=fake_tasks)
    with patch("requests.get", return_value=resp):
        tasks = integrations.fetch_jellyfin_running_tasks("http://jellyfin:8096", "key")
    assert tasks == ["Trickplay Image Extraction"]


def test_fetch_jellyfin_running_tasks_degrades_to_empty_on_failure():
    assert integrations.fetch_jellyfin_running_tasks("http://localhost:1", "key") == []


def test_high_load_thresholds_defaults(isolated_db):
    assert integrations.high_load_thresholds() == {"cpu_percent": 90, "disk_io_mbs": 150, "network_mbs": 80}


def test_evaluate_high_load_merges_jellyfin_activity(isolated_db):
    integrations._jellyfin_activity_cache["transcoding"] = 0
    integrations._jellyfin_activity_cache["running_tasks"] = []
    snapshot = {"cpu_percent": 10, "disks": [], "network": None}
    assert integrations.evaluate_high_load(snapshot) == {"active": False, "reasons": []}

    integrations._jellyfin_activity_cache["transcoding"] = 3
    result = integrations.evaluate_high_load(snapshot)
    assert result["active"] is True
    assert "3 active transcode(s)" in result["reasons"]

    integrations._jellyfin_activity_cache["transcoding"] = 0
    integrations._jellyfin_activity_cache["running_tasks"] = ["Trickplay Image Extraction"]
    result = integrations.evaluate_high_load(snapshot)
    assert result["active"] is True
    assert any("Trickplay" in r for r in result["reasons"])
