"""Unified search for signed-in visitors: the search page, live results, the detail
panel and the Seerr request flow. See routes/__init__.py for how this module
attaches to the app."""
import hashlib
import time

from flask import flash, redirect, render_template, request, session, url_for

import db
import media_search
from app import app, current_user, user_login_required

# ---------------------------------------------------------------------------
# Unified search (signed-in visitors only)
# ---------------------------------------------------------------------------
# The search rate limit and concurrency cap live in media_search.outbound_call(): per
# person (the Jellyfin user id), not process-global like the login and report limiters,
# and server-side rather than in the session cookie, where replaying an old cookie
# reset it.
# Typing "dune" shouldn't search TMDB for "d". Enforced on both sides: the client to
# avoid the request, the server because the client can't be trusted to.
MIN_LIVE_QUERY_LENGTH = 3

# How long a just-submitted request is remembered, so a double-click, a refresh, or a
# back-and-resubmit can't ask Seerr for the same thing twice. This is the middle of
# three layers, and it exists because the other two can't cover this case: the
# configuration page disables its own submit button (which stops the double-click
# before a second POST is ever sent) and Seerr's own duplicate check answers 409 (which
# stops it across devices and sessions) - but a browser resending a POST minutes later
# reaches neither. Kept in the session rather than a module-level dict because the
# meaningful unit is one person's browser: this guards against a resubmission, not
# against abuse, which is what media_search.outbound_call()'s server-side limit is for.
REQUEST_REPEAT_WINDOW_SECONDS = 60
# Bounded because the session is a cookie: this rides along on every response, so it
# must not grow with how much somebody searches for.
MAX_REMEMBERED_REQUESTS = 20


def _request_recently_submitted(key):
    """True when this exact item was already submitted a moment ago from this session.

    Prunes as it reads rather than on a timer - the whole structure is at most
    MAX_REMEMBERED_REQUESTS entries and is walked once per request submission."""
    now = time.time()
    recent = {k: t for k, t in (session.get("recent_requests") or {}).items()
              if now - t < REQUEST_REPEAT_WINDOW_SECONDS}
    already = key in recent
    if not already:
        recent[key] = now
        # Oldest first, so what falls off the end is what is least likely to still be
        # somebody's stuck submit button.
        if len(recent) > MAX_REMEMBERED_REQUESTS:
            recent = dict(sorted(recent.items(), key=lambda kv: kv[1])[-MAX_REMEMBERED_REQUESTS:])
    session["recent_requests"] = recent
    return already


def _forget_submitted_request(key):
    """Lets a genuinely failed submission be retried immediately.

    Without this, a Seerr that was down for one press would lock that title out for the
    whole window - the guard is there to stop a *successful* request being made twice,
    not to punish a failed one."""
    recent = dict(session.get("recent_requests") or {})
    if recent.pop(key, None) is not None:
        session["recent_requests"] = recent


@app.route("/search")
@user_login_required
def search():
    """The one request handler in this app that makes a live outbound call with no
    cache in front of it at all, by necessity rather than convenience.

    A search query isn't known until someone types it, so there's nothing to
    pre-fetch into a cache the way almost every other read here works - see
    media_search.py for the safety machinery that makes this carve-out acceptable.
    (A handful of other routes also make live outbound calls - a sanctioned
    one-shot admin action like "Check now" or a host/VM control, or a lookup this
    app deliberately keeps a short cache in front of, like the /account page's
    Seerr contact lookup - but none of those are an *uncached* live call the way
    every search keystroke here is.)"""
    user = current_user()
    query = request.args.get("q", "").strip()[:100]
    outcome = {"results": [], "errors": {}, "available": media_search.is_available()}
    limited = False

    if query:
        with media_search.outbound_call(user["id"]) as allowed:
            if allowed:
                outcome = media_search.search(query, jellyfin_user_id=user["id"])
        limited = not allowed

    return render_template("search.html", query=query, outcome=outcome,
                            rate_limited=limited,
                            can_request=media_search.seerr_integration() is not None,
                            jellyfin_url=media_search.jellyfin_item_url,
                            min_query_length=MIN_LIVE_QUERY_LENGTH,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/search/live")
@user_login_required
def search_live():
    """The results block alone, for the incremental search that runs while typing.

    A server-rendered HTML fragment rather than JSON, matching /api/incidents/more and
    the maintenance history: this app has exactly one JSON API (/api/status, for
    external consumers) and no client-side templating anywhere, so a fragment keeps the
    live results and the submitted results provably identical - they're the same
    template.

    Behind the same sign-in and the same rate limit as /search, because it makes
    exactly the same outbound calls."""
    user = current_user()
    query = request.args.get("q", "").strip()[:100]
    outcome = {"results": [], "errors": {}, "available": media_search.is_available()}
    limited = False

    # Below the minimum, answer with nothing rather than searching two APIs for "a".
    # The client enforces this too; this is the half that can't be bypassed.
    if len(query) >= MIN_LIVE_QUERY_LENGTH:
        with media_search.outbound_call(user["id"]) as allowed:
            if allowed:
                outcome = media_search.search(query, jellyfin_user_id=user["id"])
        limited = not allowed

    return render_template("sections/_search_results.html", query=query, outcome=outcome,
                            rate_limited=limited,
                            can_request=media_search.seerr_integration() is not None,
                            jellyfin_url=media_search.jellyfin_item_url)


@app.route("/search/detail/<media_type>/<int:tmdb_id>")
@user_login_required
def search_detail(media_type, tmdb_id):
    """A single result's full detail - poster, overview, genres, runtime, rating -
    reached by clicking a title in the search results. Counts against the same
    rate limit as searching, since it's another live outbound call.

    in_library/jellyfin_id/requested arrive as query params from the results link
    rather than being re-derived here - this route has no independent way to know a
    single TMDB id's Jellyfin/Seerr status without a second live search, and they're
    read-only display info, not anything this route writes."""
    with media_search.outbound_call(current_user()["id"]) as allowed:
        detail = media_search.detail(media_type, tmdb_id) if allowed else None
    if not allowed:
        flash("You've made a lot of requests just now - give it a minute.", "error")
        return redirect(url_for("search", q=request.args.get("q", "")))
    if not detail:
        return render_template("error.html", code=404,
                               message="Couldn't find that title."), 404
    item = {
        "title": detail["title"], "year": detail["year"], "media_type": media_type,
        "tmdb_id": tmdb_id, "poster_path": detail["poster_path"],
        "in_library": request.args.get("in_library") == "1",
        "jellyfin_id": request.args.get("jellyfin_id") or "",
        "requested": request.args.get("requested") == "1",
    }
    return render_template("search_detail.html", item=item, detail=detail,
                            query=request.args.get("q", ""),
                            can_request=media_search.seerr_integration() is not None,
                            jellyfin_url=media_search.jellyfin_item_url,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/search/request/configure")
@user_login_required
def search_request_configure():
    """Shows what a request will actually contain before submitting it, rather than
    submitting with Seerr's silent defaults - a season picker for everyone signed in,
    plus root folder/quality profile/tags only when the browser is *also* signed in as
    the portal admin (session["logged_in"]), since those reveal server filesystem
    paths an ordinary visitor has no business seeing.

    Makes live calls to Seerr (and, for the admin, its *Arr servers), so it's behind
    the same rate limit and concurrency cap as searching - it used to have neither."""
    media_type = request.args.get("media_type", "")
    tmdb_id = request.args.get("tmdb_id", "")
    is_admin = bool(session.get("logged_in"))
    with media_search.outbound_call(current_user()["id"]) as allowed:
        config = (media_search.request_configuration(media_type, tmdb_id, include_admin_fields=is_admin)
                  if allowed else None)
    if not allowed:
        flash("You've made a lot of requests just now - give it a minute.", "error")
        return redirect(url_for("search", q=request.args.get("q", "")))
    if not config:
        return render_template("error.html", code=404, message="Couldn't find that title."), 404
    return render_template("search_request_configure.html", config=config,
                            media_type=media_type, tmdb_id=tmdb_id,
                            query=request.args.get("q", ""), is_admin=is_admin,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/search/request", methods=["POST"])
@user_login_required
def search_request():
    """Asks Seerr for something on the signed-in visitor's behalf.

    A write against another service, so it's a POST, it's CSRF-protected (see
    _csrf_required_for), it requires a signed-in visitor, and it counts against the same
    rate limit and concurrency cap as searching - checked first, before anything else,
    exactly as before they moved server-side."""
    user = current_user()
    with media_search.outbound_call(user["id"]) as allowed:
        if not allowed:
            flash("You've made a lot of requests just now - give it a minute.", "error")
            return redirect(url_for("search", q=request.form.get("q", "")))
        return _submit_search_request(user)


def _submit_search_request(user):
    """search_request()'s body, run while it holds its outbound slot."""
    media_type = request.form.get("media_type", "")
    tmdb_id = request.form.get("tmdb_id", "")

    # Every numeric field is validated rather than filtered. Dropping what doesn't parse
    # is what the season list used to do, and it turns a mangled submission into a
    # *partial* one - a series quietly requested with three of its five seasons, with
    # nothing anywhere saying so. Refusing says what happened. Parsed with
    # db.parse_int(), not str.isdigit(), which let "²" through to an int() that raised.
    raw_seasons = request.form.getlist("seasons")
    seasons = None
    if media_type == "tv":
        seasons = [db.parse_int(s) for s in raw_seasons]
        if None in seasons:
            flash("That season selection didn't make sense - try again.", "error")
            return redirect(url_for("search", q=request.form.get("q", "")))

    root_folder = request.form.get("root_folder") or None
    profile_id = request.form.get("profile_id") or None
    tags = request.form.getlist("tags") or None
    # Root folder/profile/tags are only ever honoured when the browser is also signed
    # in as the portal admin - the configuration page never renders those fields for
    # anyone else, but a forged POST must not be able to smuggle them in regardless of
    # what the form actually showed.
    if not session.get("logged_in"):
        root_folder = profile_id = tags = None
    # Validated with db.parse_int() - str.isdigit() let "²" through to the int() in
    # request_via_seerr(), which raised - and then passed on as submitted.
    if (profile_id is not None and db.parse_int(profile_id) is None) or \
            (tags is not None and any(db.parse_int(t) is None for t in tags)):
        flash("That request configuration didn't make sense - try again.", "error")
        return redirect(url_for("search", q=request.form.get("q", "")))

    # Keyed on what is actually being asked for, so requesting two different seasons of
    # the same series in quick succession is still two requests - it is the *same*
    # submission arriving twice that must not become two rows in Seerr.
    #
    # Hashed because this lives in the session, and the session is a cookie: a series
    # with thirty seasons would otherwise write a ~120-character key, and a forged POST
    # could name a tmdb_id of any length at all. A fixed 16 characters keeps the whole
    # structure comfortably inside the 4KB a cookie gets, whatever is submitted.
    submission = hashlib.sha256(
        f"{media_type}:{tmdb_id}:{','.join(sorted(raw_seasons))}".encode()).hexdigest()[:16]
    if _request_recently_submitted(submission):
        flash("That was already sent a moment ago - check Seerr before asking again.",
              "error")
        return redirect(url_for("search", q=request.form.get("q", "")))

    ok, message = media_search.request(media_type, tmdb_id,
                                        user["id"], user.get("name", ""),
                                        seasons=seasons, root_folder=root_folder,
                                        profile_id=profile_id, tags=tags)
    if not ok:
        _forget_submitted_request(submission)
    flash(message, "success" if ok else "error")
    return redirect(url_for("search", q=request.form.get("q", "")))
