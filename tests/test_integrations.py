from unittest.mock import Mock, patch

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
