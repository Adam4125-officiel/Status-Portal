"""
media_search.py — one search box over Jellyfin and Seerr, and requesting what's missing.

Why this is allowed to make a live outbound call
------------------------------------------------
Every other outbound call in this app is polled into a cache by a background task,
because a request handler must never wait on another server. Search is the one genuine
exception: the query isn't known until somebody types it, so there is nothing to
pre-fetch. That makes this a deliberate carve-out rather than an oversight, and it
carries its own safety machinery:

- **Its own short timeout** (`config.SEARCH_TIMEOUT_SECONDS`, 6s), well under every
  other timeout here, so a slow Jellyfin can't tie up a request-handling thread.
- **A clear degraded state.** Each source fails independently; one being down returns
  the other's results plus a note, and both being down returns "search is unavailable
  right now" rather than an error page.
- **Signed-in visitors only, plus a per-session rate limit** (enforced in app.py). The
  result set reveals the whole library, requesting is a write against Seerr that has to
  be attributable to a person, and a search box wired to two external APIs is otherwise
  a free denial-of-service amplifier.

Whose Seerr account a request goes through
------------------------------------------
The matching Seerr user's, when a real link exists - Seerr's own `jellyfinUserId`, the
same link per-user notifications follow, established when Seerr imported the Jellyfin
accounts. That is what makes Seerr's approval queue say who actually asked.

With no link, the request is still made (via the API key's own account) but the portal
records who asked in its log, and the UI says the request will show up unattributed.
The link is never *guessed* from a matching email or username: getting that wrong would
attribute one person's request to another, and an unattributed request is a much
smaller problem than a misattributed one.
"""
import logging

import requests

import db
import integrations

_logger = logging.getLogger(__name__)


def jellyfin_integration():
    return next((i for i in db.list_integrations()
                 if i["kind"] == "jellyfin" and i["enabled"]), None)


def seerr_integration():
    """Whichever Seerr the admin selected - see integrations.seerr_integration().

    Deliberately delegated rather than re-implemented here: three copies of "the first
    enabled one" is three chances for the search page, the notifier and the diagnostic
    to end up talking to different servers without anyone noticing."""
    return integrations.seerr_integration()


def is_available():
    """Search needs at least one of the two to be configured to mean anything."""
    return jellyfin_integration() is not None or seerr_integration() is not None


def _key(item):
    """What counts as "the same thing" across the two sources.

    Title plus year, case-folded. Neither source shares an id with the other - Jellyfin
    has its own ids and Seerr speaks TMDB - so the title is all there is to match on.
    Year is included because remakes are common and merging a 1974 film with its 2024
    remake would be worse than showing both."""
    return (item["title"].strip().casefold(), item.get("year"))


def merge(jellyfin_items, seerr_items):
    """One list, no duplicates, in-library first.

    A title found in both sources appears once, keeping Seerr's richer metadata (the
    overview, the TMDB id needed to request it) while inheriting Jellyfin's proof that
    it's actually there and the id needed to link to it."""
    merged = {}
    for item in jellyfin_items:
        merged[_key(item)] = dict(item)
    for item in seerr_items:
        key = _key(item)
        if key in merged:
            existing = merged[key]
            existing.update({k: v for k, v in item.items()
                              if k in ("tmdb_id", "overview", "poster_path")})
            # Jellyfin having it is proof; Seerr's opinion of availability isn't.
            existing["in_library"] = True
            existing["source"] = "both"
        else:
            merged[key] = dict(item)
    return sorted(merged.values(),
                   key=lambda i: (not i["in_library"], i["title"].casefold()))


def search(query, jellyfin_user_id=None):
    """Both sources, merged. Never raises - a failed source becomes an error string.

    Returns {"results": [...], "errors": {source: message}, "available": bool}, where
    `available` is False only when nothing could be searched at all, which is what the
    page turns into "search is unavailable right now"."""
    query = (query or "").strip()
    result = {"results": [], "errors": {}, "available": False}
    if not query:
        return result

    jellyfin, seerr = jellyfin_integration(), seerr_integration()
    jellyfin_items, seerr_items = [], []

    if jellyfin:
        try:
            jellyfin_items = integrations.search_jellyfin(
                jellyfin["base_url"], jellyfin["api_key"], query, jellyfin_user_id)
            result["available"] = True
        except (requests.RequestException, ValueError) as e:
            # Warning, not info: this is a user-visible failure, and it used to be
            # logged quietly enough that nobody could tell *why* search had degraded.
            _logger.warning("Jellyfin search failed: %s", e)
            result["errors"]["Jellyfin"] = integrations.describe_request_error(e)

    if seerr:
        try:
            seerr_items = integrations.search_seerr(seerr["base_url"], seerr["api_key"], query)
            result["available"] = True
        except (requests.RequestException, ValueError) as e:
            _logger.warning("Seerr search failed: %s", e)
            result["errors"]["Seerr"] = integrations.describe_request_error(e)

    result["results"] = merge(jellyfin_items, seerr_items)
    return result


def seerr_user_id_for(jellyfin_user_id):
    """The Seerr account id to attribute a request to, or None.

    Prefers the id already stored on the user's preferences (put there when they linked
    their account), and falls back to asking Seerr directly - both of which follow
    Seerr's own `jellyfinUserId`. Never guesses; see the module docstring."""
    stored = db.get_user_preferences(jellyfin_user_id).get("seerr_user_id")
    if stored:
        return stored
    seerr = seerr_integration()
    if not seerr or not jellyfin_user_id:
        return None
    try:
        users = integrations.fetch_seerr_users(seerr["base_url"], seerr["api_key"])
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not resolve a Seerr account for the request: %s", e)
        return None
    match = next((u for u in users if u["jellyfin_user_id"] == str(jellyfin_user_id)), None)
    if match:
        # Remembered so the next request doesn't need the lookup, and so the account
        # page shows the link.
        db.set_user_preferences(jellyfin_user_id, seerr_user_id=match["id"])
        return match["id"]
    return None


def jellyfin_item_url(item_id):
    """A deep link straight to the item in Jellyfin's own web client.

    Built from the integration's base URL, which is what the portal already knows. That
    is usually also what a visitor can reach, but not always (a portal talking to
    Jellyfin over a LAN address while visitors come in through a domain), so this is a
    convenience link rather than a guarantee - which is why the result also stays
    useful without it."""
    integration = jellyfin_integration()
    if not integration or not item_id:
        return ""
    return f"{integration['base_url'].rstrip('/')}/web/index.html#!/details?id={item_id}"


def request(media_type, tmdb_id, jellyfin_user_id, jellyfin_user_name=""):
    """Asks Seerr for something. Returns (ok, message).

    Never raises: this is reached from a form POST, and every outcome an ordinary person
    can cause - already requested, Seerr down, no permission - has to come back as a
    sentence rather than a traceback."""
    seerr = seerr_integration()
    if seerr is None:
        return False, "Requesting isn't set up on this portal."
    if media_type not in ("movie", "tv"):
        return False, "That isn't something that can be requested."
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return False, "That isn't a valid item."

    seerr_user_id = seerr_user_id_for(jellyfin_user_id)
    try:
        integrations.request_via_seerr(seerr["base_url"], seerr["api_key"],
                                        media_type, tmdb_id, seerr_user_id)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 409:
            # Seerr's own "already requested", which is a perfectly ordinary thing to
            # hit by pressing the button twice - not an error worth alarming anyone with.
            return False, "That has already been requested."
        _logger.warning("Seerr refused a request from '%s': %s", jellyfin_user_name, e)
        return False, f"Seerr refused the request ({status})."
    except (requests.RequestException, ValueError) as e:
        _logger.warning("Could not reach Seerr to make a request: %s", e)
        return False, "Couldn't reach Seerr just now - try again in a moment."

    if seerr_user_id:
        return True, "Requested. You'll be notified when it arrives."
    # Recorded here because Seerr itself can't say who asked in this case.
    _logger.info("Seerr request for %s/%s made by Jellyfin user '%s' with no linked "
                 "Seerr account - it will appear unattributed in Seerr",
                 media_type, tmdb_id, jellyfin_user_name)
    return True, ("Requested. Your Jellyfin account isn't linked to a Seerr one, so it "
                  "will show up in Seerr without your name on it.")
