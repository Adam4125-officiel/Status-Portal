"""The background health check: running each service's check, folding linked
integrations and dependencies into its status, and opening/resolving automatic
incidents. Also the integration-status cache the loop fills, the low-disk alert it
raises, and the daily TLS certificate check.

app.py imports this module, never the other way round: it reads the caches kept
here and starts the loop with start_background_checker(). Must not import app."""

import logging
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import config
import db
import integrations
import monitoring
import notifications
import scheduler
import user_notify

_logger = logging.getLogger(__name__)

# Reference point for each service's startup_grace_seconds - "time since the portal
# process started", not since the service itself booted (good enough since they're
# expected to start around the same time). monotonic() so a system clock change
# can't skew it.
_APP_START = time.monotonic()

# Service ids currently mid-retry-loop in _check_service_status() (see below) - read
# by routes/public.py's index() so a public-page hit during that window can show "retrying" instead of
# silently keeping the last-known status.
_retry_in_progress = set()

# Integration statuses are checked in the background (see run_health_checks) and cached
# here, keyed by integration id. Nothing in the request path ever calls
# integrations.fetch_integration_status() directly - a slow/unreachable integration
# (confirmed: ~10s for an unresponsive *Arr host, due to its v3->v1 fallback, each with
# a 5s timeout) would otherwise block every single page load, including every public
# auto-refresh cycle. That was a real bug: the public page appeared to be "stuck
# refreshing" because it was, in fact, blocked on a slow outbound HTTP call every time.
_integration_status_cache = {}


def _fetch_integration_entry(integ):
    """One integration's cache entry, stamped when its own answer arrived. Never raises
    - a failure is an unreachable status, exactly as it always was."""
    try:
        status = integrations.fetch_integration_status(integ)
    except Exception as e:
        status = {"reachable": False, "version": None, "issues": [], "error": str(e)}
    return {"status": status, "checked_at": db.now_iso()}


def _refresh_integration_cache():
    integrations_list = db.list_integrations()
    enabled = [integ for integ in integrations_list if integ["enabled"]]
    # The network calls run in parallel, bounded like the service checks in run_health_checks():
    # one after another, a down *Arr (v3 then v1) cost 10s, a down Byparr 30s, and a
    # few of those pushed the cycle well past CHECK_INTERVAL_SECONDS. Everything that
    # acts on the answers - the cache write and the incident lifecycle - stays
    # sequential, in list order, below: only the waiting is parallel, not a decision.
    with ThreadPoolExecutor(max_workers=config.HEALTH_CHECK_WORKERS) as pool:
        entries = list(pool.map(_fetch_integration_entry, enabled))
    for integ, entry in zip(enabled, entries):
        previous = _integration_status_cache.get(integ["id"])
        status = entry["status"]
        _integration_status_cache[integ["id"]] = entry
        if integ["auto_incident"] and integ["service_id"] and previous is not None:
            linked_service = db.get_service(integ["service_id"])
            if not linked_service or not _within_grace_period(linked_service):
                _handle_integration_incident_lifecycle(integ, previous["status"]["reachable"], status["reachable"])

    # High-load detection wants a couple of Jellyfin-specific signals (active
    # transcode count, running scheduled tasks like trickplay generation) beyond
    # plain reachability - refreshed here (background loop) for the same reason as
    # everything else in this function: never queried live from a request handler.
    jellyfin = next((i for i in integrations_list if i["kind"] == "jellyfin" and i["enabled"]), None)
    if jellyfin:
        try:
            integrations.refresh_jellyfin_activity_cache(jellyfin["base_url"], jellyfin["api_key"])
        except Exception:
            _logger.exception("Jellyfin activity refresh failed")


def clear_caches():
    """Drops the integration-status cache. Called by the admin panel's clear-caches
    button and between tests; the next health-check cycle refills it."""
    _integration_status_cache.clear()


def _service_list_phrase(names):
    """"A", "A and B", "A, B and C" - for the sentence body of a maintenance
    notification, which reads as prose rather than as the bare comma-separated
    prefix an incident notification uses."""
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _process_maintenance_and_notify():
    """Also called directly from routes/admin_content.py's admin_maintenance_new()/admin_maintenance_edit()
    (a request handler), not just from the health-check loop below - db.process_
    maintenance_windows() above already applied the actual status flip by the time
    this loop runs, so backgrounding only the notify() call doesn't delay that; see
    those routes' own comments for why the flip itself has to stay synchronous.

    db.process_maintenance_windows() reports one event *per service*, because that is
    what the status flip operates on. Notifications are grouped back up to one per
    window and per transition before anything is sent: a window covering five services
    is one thing that happened, and announcing it five times over is how an admin's
    inbox (and every opted-in visitor's) gets five identical-looking emails in a row."""
    groups = {}
    for event in db.process_maintenance_windows():
        key = (event["window"]["id"], event["event"])
        group = groups.setdefault(key, {"window": event["window"],
                                         "started": event["event"] == "maintenance_started",
                                         "names": []})
        group["names"].append(event["service"]["name"])

    for group in groups.values():
        window_title = group["window"]["title"]
        names = group["names"]
        started = group["started"]
        title = "Maintenance started" if started else "Maintenance ended"
        listed = ", ".join(names)
        phrase = _service_list_phrase(names)
        verb = "is" if len(names) == 1 else "are"
        notifications.notify_async(title, f"{listed}: {window_title}")
        # And the visitors who asked to hear about service events. Queued from this
        # background thread exactly as it would be from a request handler - the queue
        # is what keeps delivery off whichever thread noticed the event.
        user_notify.notify_service_subscribers(
            "maintenance",
            f"{title}: {listed}",
            (f"{phrase} {verb} now under maintenance ({window_title})."
             if started else
             f"Maintenance on {phrase} has finished ({window_title})."))


def _check_status_for_response(r, elapsed_ms, slow_threshold_ms):
    """A response was received at all, so the service is reachable - only a 5xx
    (actual server-side error) counts against it. Anything else the server sends
    back, including a 401/403 auth challenge (e.g. Bazarr's login prompt) or a 404,
    means it answered and is up. 'Slow' is purely a latency observation layered on
    top of an otherwise-healthy response, never on top of a 5xx.

    502 specifically means "down", not "degraded": unlike a 500/503/504 (the
    service itself is up but erroring/overloaded), a 502 means whatever's in
    front of it (reverse proxy, gateway) couldn't even reach it - functionally
    equivalent to a connection failure from the visitor's perspective."""
    if r.status_code == 502:
        return "down"
    if r.status_code >= 500:
        return "degraded"
    if slow_threshold_ms and elapsed_ms > slow_threshold_ms:
        return "slow"
    return "operational"


def _within_grace_period(service):
    """True while a service is still inside its configured post-startup grace
    window (time since this process started, not since the service itself booted -
    good enough since they're expected to start around the same time)."""
    grace = service.get("startup_grace_seconds") or 0
    return grace > 0 and (time.monotonic() - _APP_START) < grace


TCP_CHECK_TIMEOUT_SECONDS = 5


def _split_host_port(target):
    """(host, port) from a 'host:port' string, or None when it is not one. Accepts
    '[::1]:80' for IPv6 and tolerates a leading scheme or a trailing path, which people
    paste out of habit."""
    target = str(target or "").strip()
    if "://" in target:
        target = target.split("://", 1)[1]
    target = target.split("/", 1)[0]
    host, sep, port = target.rpartition(":")
    host = host.strip("[]")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host, int(port)


def _run_tcp_check(target, slow_threshold_ms):
    """One TCP attempt: is the port open? Returns (status, elapsed_ms), the same shape
    and the same states as the HTTP check - connected is operational (slow past the
    threshold), anything else, including a target that is not host:port, is down."""
    endpoint = _split_host_port(target)
    if endpoint is None:
        return "down", None
    start = time.perf_counter()
    try:
        with socket.create_connection(endpoint, timeout=TCP_CHECK_TIMEOUT_SECONDS):
            pass
    except OSError:
        return "down", None
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if slow_threshold_ms and elapsed_ms > slow_threshold_ms:
        return "slow", elapsed_ms
    return "operational", elapsed_ms


def _run_single_check(check_url, slow_threshold_ms, check_type="http"):
    """One attempt against check_url (an HTTP request, or a TCP connect to host:port
    when check_type is "tcp"). Returns (status, elapsed_ms).

    Streamed, and closed as soon as the status line and headers are in: the verdict
    only ever depends on the status code (see _check_status_for_response), so
    downloading the body - a whole web app's HTML, every cycle, for every service -
    was pure waste. elapsed_ms is therefore time to the response headers, not to
    the last byte of the body."""
    if check_type == "tcp":
        return _run_tcp_check(check_url, slow_threshold_ms)
    start = time.perf_counter()
    try:
        r = requests.get(check_url, timeout=5, stream=True)
        try:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return _check_status_for_response(r, elapsed_ms, slow_threshold_ms), elapsed_ms
        finally:
            r.close()
    except requests.RequestException:
        return "down", None


def _check_service_status(service):
    """Runs a service's health check, retrying only a 'down' result - a degraded/
    slow/operational result is never worth retrying, and only 'down' ever opens an
    auto-incident (see _handle_incident_lifecycle), so retrying anything else would
    just delay the loop for no benefit. `retry_count` extra attempts are made
    (0 = disabled, the historical single-attempt behavior), spaced
    `retry_interval_seconds` apart; the first non-down result wins immediately, so a
    service that recovers seconds later never triggers a spurious incident. Runs
    inline in the background health-check thread - not a request handler, so
    blocking here for the retry window is the intended tradeoff, not a bug.

    While actually mid-retry (sleeping between attempts), the service's id is kept
    in _retry_in_progress so a public-page hit during that window can show
    "retrying" instead of silently keeping the last-known status - narrow (only
    matters if a request lands in the same seconds-long window), but real, since
    this loop can block for up to retry_count * retry_interval_seconds."""
    check_type = service.get("check_type") or "http"
    status, elapsed_ms = _run_single_check(service["check_url"], service["slow_threshold_ms"], check_type)
    retries_left = service.get("retry_count") or 0
    interval = service.get("retry_interval_seconds") or 0
    try:
        while status == "down" and retries_left > 0:
            _retry_in_progress.add(service.get("id"))
            time.sleep(interval)
            status, elapsed_ms = _run_single_check(service["check_url"], service["slow_threshold_ms"], check_type)
            retries_left -= 1
    finally:
        _retry_in_progress.discard(service.get("id"))
    return status, elapsed_ms


def _merge_api_health(status, api_health_mode, integration_reachable):
    """Folds a linked integration's cached API reachability into a service's own
    web-check status, producing the single final status used for both the public
    display and _handle_incident_lifecycle below - deliberately not two separate
    decisions, to preserve the "one status feeds both" invariant documented on
    _handle_incident_lifecycle (a past bug here already came from letting a status
    write and an incident-lifecycle decision drift apart).

    integration_reachable is None when there's nothing to act on (api_health_mode
    is "off", or this service has no enabled+publicly-shown linked integration) -
    the web-check status passes through unchanged. "down" mode always wins outright
    once the integration is unreachable; "degrade" only ever raises operational/slow
    up to degraded, never overrides an already-worse status like "down"."""
    if api_health_mode == "off" or integration_reachable is None or integration_reachable:
        return status
    if api_health_mode == "down":
        return "down"
    if api_health_mode == "degrade" and status in ("operational", "slow"):
        return "degraded"
    return status


def _linked_integration_reachable(service_id):
    """None = nothing to act on (no enabled+publicly-shown integration linked to
    this service, or it hasn't been checked yet); True/False = that integration's
    last cached reachability. Reuses the same enabled+show_on_public integrations
    already surfaced on this service's own public card (db.list_integrations_for_service)
    on purpose - api_health_mode only makes sense in relation to an integration a
    visitor can actually see the sub-badge for, not a hidden one, so a status bump
    never appears to come from nowhere."""
    linked = db.list_integrations_for_service(service_id)
    if not linked:
        return None
    cached = _integration_status_cache.get(linked[0]["id"])
    if not cached:
        return None
    return cached["status"]["reachable"]


def _merge_dependency_health(status, dependency_statuses):
    """Folds this service's direct dependencies' status into its own, same
    non-destructive precedence and "one status feeds both display and incident
    lifecycle" reasoning as _merge_api_health above: only ever raises
    operational/slow up to degraded, never overrides an already-worse down/
    maintenance. Only a dependency that's actually 'down' triggers this - a merely
    degraded dependency is still serving requests, not a strong enough signal to
    cascade further.

    Direct dependencies only, not transitive - deliberately. Resolving a chain
    would need real cycle detection and topological ordering that nothing here
    currently needs; a one-hop-only merge can't infinite-loop even if two services
    are configured to depend on each other."""
    if status not in ("operational", "slow"):
        return status
    if "down" in dependency_statuses:
        return "degraded"
    return status


TLS_CHECK_TIMEOUT_SECONDS = 5
DEFAULT_TLS_ALERT_DAYS = 14


def _tls_alert_days():
    return db.parse_int(db.get_setting("tls_expiry_alert_days", "")) or DEFAULT_TLS_ALERT_DAYS


def _peer_cert_expiry(host, port, context):
    with socket.create_connection((host, port), timeout=TLS_CHECK_TIMEOUT_SECONDS) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True) if context.verify_mode == ssl.CERT_NONE \
                else tls.getpeercert()


def _read_cert_expiry(host, port):
    """When the certificate served at host:port expires (aware UTC datetime), or None
    when it cannot be read. Never raises. A certificate that does not verify is still
    read - an internal service with a self-signed certificate expires like any other -
    by fetching it unverified and trusting exactly that certificate for a second,
    decoded handshake (the standard library only decodes verified certificates). An
    already-expired certificate fails verification with a specific code and is
    reported as expiring now."""
    try:
        context = ssl.create_default_context()
        try:
            cert = _peer_cert_expiry(host, port, context)
        except ssl.SSLCertVerificationError as e:
            if e.verify_code == 10:  # X509_V_ERR_CERT_HAS_EXPIRED
                return datetime.now(timezone.utc)
            loose = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            loose.check_hostname = False
            loose.verify_mode = ssl.CERT_NONE
            der = _peer_cert_expiry(host, port, loose)
            pinned = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
            pinned.check_hostname = False
            cert = _peer_cert_expiry(host, port, pinned)
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def _https_endpoint(service):
    """(host, port) of an auto-checked HTTP service whose check URL is https, or None."""
    if (service.get("check_type") or "http") != "http" or not service["auto_check"]:
        return None
    parsed = urlparse(service["check_url"] or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        return parsed.hostname, parsed.port or 443
    except ValueError:
        return None


def _check_tls_certificates():
    """Edge-triggered like the low-disk alert: one notification when a certificate
    first falls inside the warning window, nothing more until it has been renewed
    (back outside the window) and runs low again. The warned/not-warned state is in
    the database, so a restart cannot repeat a warning. Notification-only; the
    expiry date is shown on the admin Services page, never publicly."""
    window = _tls_alert_days()
    checked = 0
    for s in db.list_services():
        endpoint = _https_endpoint(s)
        if endpoint is None:
            continue
        checked += 1
        expires = _read_cert_expiry(*endpoint)
        if expires is None:
            continue  # unreachable today - keep the last known state
        days_left = (expires - datetime.now(timezone.utc)).days
        db.set_tls_expiry(s["id"], expires.date().isoformat())
        inside = days_left < window
        if inside and not db.get_tls_alert_state(s["id"]):
            when = ("has expired" if expires <= datetime.now(timezone.utc)
                    else f"expires on {expires.date().isoformat()} ({days_left} day(s) left)")
            notifications.notify("TLS certificate expiring",
                                  f"The certificate for {s['name']} {when}.")
        db.set_tls_alert_state(s["id"], inside)
    return checked


def _tls_certificates_task():
    checked = _check_tls_certificates()
    if not checked:
        raise scheduler.TaskSkipped("No service is checked over HTTPS.")
    return f"Read the certificate of {checked} HTTPS service(s)."


def register_tasks():
    """Registers this module's scheduled task. Called by app.py at the point where
    the task used to be registered, rather than at this module's import, so the task
    keeps its place in /admin/tasks (registration order)."""
    scheduler.register(
        "tls_certificates",
        "Check TLS certificate expiry",
        "Reads the certificate of every service checked over HTTPS once a day and sends a "
        "notification, once, when one has fewer days left than the limit set under "
        "Settings. The expiry date shows on the Services page.",
        _tls_certificates_task,
        default_schedule_kind="daily",
        default_daily_at="04:00",
    )


def _lowdisk_threshold():
    return db.parse_int(db.get_setting("lowdisk_percent_threshold", ""))


def _check_low_disk_space(snapshot):
    """Edge-triggered: notifies once when a disk crosses the threshold, and once
    when it recovers - never every cycle while it stays low, which would spam a
    notification every CHECK_INTERVAL_SECONDS forever. Notification-only, no
    incident - incidents are inherently tied to a service via the join table, and
    disk space isn't a service.

    State is persisted via db.get/set_low_disk_alert_state() (SQLite, not just
    in-process memory) specifically so a portal restart while a disk is still low
    doesn't re-fire the "low disk space" notification - this app is meant to
    survive its own restart cleanly (see CLAUDE.md)."""
    threshold = _lowdisk_threshold()
    if not threshold:
        return
    low_disks = {d["path"] for d in monitoring.evaluate_low_disk(snapshot, threshold)}
    for d in snapshot.get("disks", []):
        path = d["path"]
        was_low = db.get_low_disk_alert_state(path)
        is_low = path in low_disks
        if is_low and not was_low:
            notifications.notify("Low disk space",
                                  f"{d['display_name']}: {d['percent']}% used ({d['free_gb']} GB free)")
        elif was_low and not is_low:
            notifications.notify("Disk space back to normal", f"{d['display_name']} is no longer low on space.")
        db.set_low_disk_alert_state(path, is_low)


def _check_and_record_service(s, status_by_id):
    """One service's full check-and-record - the per-service body of the loop below,
    pulled out so it can be submitted to a thread pool. Caught and logged here rather
    than left to the loop's own try/except: a bounded pool means other services'
    checks are already running concurrently regardless, so one service raising must
    not cost the rest of this cycle's results the way it would in a plain
    sequential loop with a single try/except wrapped around the whole thing."""
    try:
        if not s["auto_check"] or s["manual_override"] or not s["check_url"]:
            return
        previous_status = s["status"]
        status, elapsed_ms = _check_service_status(s)
        status = _merge_api_health(status, s.get("api_health_mode", "off"),
                                    _linked_integration_reachable(s["id"]))
        dependency_statuses = [status_by_id.get(dep_id) for dep_id in db.get_service_dependencies(s["id"])]
        status = _merge_dependency_health(status, dependency_statuses)
        db.record_check_result(s["id"], status, elapsed_ms)
        # Status/response time are recorded above regardless - only the
        # auto-incident escalation is held back during the grace window, so a
        # service that's still booting doesn't get an incident opened on it.
        if s["auto_incident"] and not _within_grace_period(s):
            _handle_incident_lifecycle(s, previous_status, status)
    except Exception:
        _logger.exception("health check failed for service '%s'", s.get("name", "?"))


HEARTBEAT_TIMEOUT_SECONDS = 5
LAST_HEALTH_CYCLE_SETTING = "last_health_cycle_at"


def _finish_health_cycle():
    """End-of-cycle bookkeeping: record when the cycle finished (shown on
    /admin/tasks) and ping PORTAL_HEARTBEAT_URL if one is set. Never raises - a dead
    heartbeat endpoint or a locked database must not stop the next cycle."""
    try:
        db.set_setting(LAST_HEALTH_CYCLE_SETTING, datetime.now(timezone.utc).isoformat())
    except Exception:
        _logger.exception("could not record the end of the health-check cycle")
    url = config.HEARTBEAT_URL
    if not url:
        return
    try:
        requests.get(url, timeout=HEARTBEAT_TIMEOUT_SECONDS).raise_for_status()
    except Exception as e:
        # Not the URL itself: heartbeat URLs usually carry a secret token.
        _logger.warning("heartbeat ping failed: %s", type(e).__name__)


def run_health_checks():
    while True:
        try:
            # Runs before the per-service checks below, so a window that just started
            # (setting manual_override=1) is already in effect for this same cycle's
            # loop - otherwise a service being intentionally taken down for maintenance
            # could get a spurious auto-incident opened in the same instant its window begins.
            _process_maintenance_and_notify()
            services = db.list_services()
            # A fixed pre-loop snapshot, not a live re-query mid-loop - otherwise
            # whether a dependency's fresh-this-cycle status is visible would
            # silently depend on iteration order (already processed vs. not yet).
            # Caps dependency staleness at one check interval, same as everything
            # else here already tolerates.
            status_by_id = {row["id"]: row["status"] for row in services}
            # Bounded to config.HEALTH_CHECK_WORKERS rather than one thread per
            # service - a service with retry_count set can hold its own slot for
            # retry_count * retry_interval_seconds seconds, and fully sequential
            # checking meant a handful of unreachable services could push a single
            # cycle well past CHECK_INTERVAL_SECONDS. Each service's dependency
            # lookup still reads the same pre-loop status_by_id snapshot regardless
            # of which worker thread runs it or in what order, which is what keeps
            # this safe to parallelize without dependency results depending on
            # thread scheduling.
            with ThreadPoolExecutor(max_workers=config.HEALTH_CHECK_WORKERS) as pool:
                # list() to actually wait for every submitted check before moving on
                # to the integration/disk checks below, which is what preserves this
                # loop's existing "everything for this cycle finishes before the
                # next one starts" shape.
                list(pool.map(lambda s: _check_and_record_service(s, status_by_id), services))
            _refresh_integration_cache()
            _check_low_disk_space(monitoring.get_resource_snapshot())
            # Pruning old history and checking GitHub for releases used to be tacked
            # on here, each with its own hand-rolled "is it due yet" check. Both are
            # scheduled tasks now (see /admin/tasks) - this loop is back to being only
            # the things that genuinely have to happen on the health-check cycle:
            # maintenance windows first, then every service's status, then the
            # integration cache that _merge_api_health() reads from.
            _finish_health_cycle()
        except Exception:
            _logger.exception("health-check loop error")
        time.sleep(config.CHECK_INTERVAL_SECONDS)


def _handle_incident_lifecycle(service, previous_status, new_status):
    """Auto-opens an incident whenever a service is down and doesn't already have one
    open, and auto-resolves it the moment it recovers. Only hard 'down' transitions
    ever resolve - 'degraded' is left for a human to judge, since a single slow
    response isn't necessarily an incident.

    The open side is deliberately level-triggered (checks new_status alone, not
    previous_status != "down") rather than edge-triggered - idempotency already
    comes from the get_open_auto_incident_for_service() guard below, and an
    edge-trigger silently breaks for a service whose status was already 'down' the
    last time this function *would* have run had a startup grace period not
    suppressed the call: services.status still gets written to 'down' every cycle
    during grace (status/response time are recorded regardless), so by the time the
    grace period elapses, previous_status is already 'down' and an edge-trigger
    would never see a fresh transition again - the service could stay down forever
    with no incident ever opening. Caught by live-testing the grace period feature
    against a real always-refusing HTTP server (2026-07-23), not by unit tests."""
    if new_status == "down":
        if not db.get_open_auto_incident_for_service(service["id"]):
            incident_id = db.create_auto_incident(
                service["id"], f"{service['name']} is unreachable", "investigating")
            db.create_incident_update(
                incident_id, "Automatic health check could not reach this service.", "investigating")
            notifications.notify("Incident opened", f"{service['name']} is unreachable.")
    elif new_status != "down" and previous_status == "down":
        incident = db.get_open_auto_incident_for_service(service["id"])
        if incident:
            notifications.notify("Incident resolved", f"{service['name']} has recovered.")
            db.update_incident(incident["id"], {
                "service_id": service["id"],
                "title": incident["title"],
                "description": incident["description"],
                "status": "resolved",
            })
            db.create_incident_update(
                incident["id"], "Automatic health check confirmed this service has recovered.", "resolved")


def _handle_integration_incident_lifecycle(integration, previous_reachable, new_reachable):
    """Same auto-open/auto-resolve pattern as _handle_incident_lifecycle, but driven by
    an integration's reachability (Jellyfin/*Arr/Jellyseerr) instead of a service's own
    health check. Shares the same incident slot as the linked service (via
    get_open_auto_incident_for_service/create_auto_incident) - if the service's own
    health check already opened one, this won't open a second.

    Same level-triggered-open reasoning as _handle_incident_lifecycle applies here -
    _integration_status_cache is also updated every cycle regardless of grace, so an
    edge-trigger on previous_reachable would have the same "never opens after a
    grace-suppressed cycle" bug."""
    service_id = integration["service_id"]
    if new_reachable is False:
        if not db.get_open_auto_incident_for_service(service_id):
            service = db.get_service(service_id)
            name = service["name"] if service else integration["name"]
            incident_id = db.create_auto_incident(
                service_id, f"{name} is unreachable", "investigating")
            db.create_incident_update(
                incident_id, f"Automatic status check for '{integration['name']}' could not reach it.",
                "investigating")
            notifications.notify("Incident opened", f"{name}: status check '{integration['name']}' failed.")
    elif new_reachable is True and previous_reachable is False:
        incident = db.get_open_auto_incident_for_service(service_id)
        if incident:
            service = db.get_service(service_id)
            name = service["name"] if service else integration["name"]
            notifications.notify("Incident resolved", f"{name} has recovered.")
            db.update_incident(incident["id"], {
                "service_id": service_id,
                "title": incident["title"],
                "description": incident["description"],
                "status": "resolved",
            })
            db.create_incident_update(
                incident["id"], f"Automatic status check for '{integration['name']}' confirmed recovery.",
                "resolved")


# The health-check thread, held only so /admin/tasks can report whether it is still
# alive. threading.excepthook already logs a thread dying from something outside its
# own try/except (see logging_setup.py), but "services just stopped updating" is
# exactly the symptom nobody thinks to look in a log file for.
_health_thread = {"thread": None}


def _health_thread_alive():
    thread = _health_thread["thread"]
    return None if thread is None else thread.is_alive()


def start_background_checker():
    t = threading.Thread(target=run_health_checks, daemon=True, name="health-checks")
    t.start()
    _health_thread["thread"] = t
    return t
