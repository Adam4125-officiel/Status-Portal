"""
admin_search.py — finding a setting anywhere in the admin panel.

The problem, and the shape that answers it
------------------------------------------
The admin panel is ~20 pages and several hundred individual controls. Knowing that a
setting exists is not the same as remembering which page it lives on, and "is the
low-disk alert under Settings or under Notifications?" is a question this panel asked
its own author regularly. So: one search box, reachable from every admin page, that
knows about every control on all of them.

**The index is derived from the templates, never hand-written.** That is the whole
design decision, and it exists because of this project's most repeated failure mode: a
list that has to be updated alongside something else, and isn't (see CLAUDE.md's "three
places, not one"). A hand-maintained index of every setting would be wrong within two
sessions, and wrong invisibly - a missing entry looks exactly like a setting that
doesn't exist, which is worse than no search at all.

Instead `build_index()` reads the admin templates and pulls out every `<label>` and
every field hint, together with the `name=` of the input each belongs to. Adding a
setting therefore adds it to the search automatically, and renaming one renames it in
the search, because there is only ever one source of truth: the template that renders
it.

The cost is that parsing HTML with regular expressions is approximate. That is
acceptable *here* and would not be elsewhere: nothing is executed, nothing is trusted,
and the worst outcome of a mis-parse is a slightly odd result title in a list the admin
is reading anyway. Every entry is escaped by Jinja on the way out like any other
untrusted text.

Why not render the pages and read the DOM
-----------------------------------------
Tempting, and wrong. Rendering an admin page has side effects - `/admin/reports` marks
messages as read, `/admin/logs` reads files off disk - so building an index that way
would mean a search box quietly marking someone's reports as read. Templates are inert.

Caching
-------
Built once and kept in a module-level cache, invalidated by the templates' own mtimes,
so an edited template is picked up without a restart (matching `asset_url()`'s
approach) while a search costs no disk I/O in the normal case.
"""
import html
import logging
import os
import re
import threading

_logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Which template belongs to which admin page. This *is* hand-maintained, and it is the
# one list that can afford to be: it has one entry per page rather than per setting, it
# changes when a page is added rather than when a setting is, and
# `test_every_admin_template_is_searchable` fails if a template is missing from it. The
# label is what the result list shows as the destination, and must match the nav (see
# CLAUDE.md - an admin page's h1, its title and its nav label all have to agree).
#
# `endpoint` is a Flask endpoint name; the search route resolves it with url_for(), so
# a moved URL follows automatically.
PAGES = [
    ("admin_services.html", "admin_services", "Services"),
    ("admin_service_form.html", "admin_service_new", "Services · New service"),
    ("admin_new_combined.html", "admin_new_combined", "Services · New service + check"),
    ("admin_new_picker.html", "admin_new_picker", "Services · Add"),
    ("admin_incidents.html", "admin_incidents", "Incidents"),
    ("admin_incident_form.html", "admin_incident_new", "Incidents · New incident"),
    ("admin_maintenance.html", "admin_maintenance", "Maintenance"),
    ("admin_maintenance_form.html", "admin_maintenance_new", "Maintenance · New window"),
    ("admin_reports.html", "admin_reports", "Reports"),
    ("admin_announcements.html", "admin_announcements", "Announcements"),
    ("admin_announcement_form.html", "admin_announcement_new", "Announcements · New"),
    ("admin_info.html", "admin_info", "Info page"),
    ("admin_integrations.html", "admin_integrations", "Integrations"),
    ("admin_integration_form.html", "admin_integration_new", "Integrations · New"),
    ("admin_resources.html", "admin_resources", "Resources"),
    ("admin_tasks.html", "admin_tasks", "Scheduled tasks"),
    ("admin_notifications.html", "admin_notifications", "Notification channels"),
    ("admin_discord_bot.html", "admin_discord_bot", "Discord bot"),
    ("admin_discord_bot_guilds.html", "admin_discord_bot_guilds", "Discord servers"),
    ("admin_user_notifications.html", "admin_user_notifications", "Per-user notifications"),
    ("admin_seerr_alerts.html", "admin_seerr_alerts", "Seerr approvals"),
    ("admin_system.html", "admin_system", "System"),
    ("admin_logs.html", "admin_logs", "Logs"),
    ("admin_settings.html", "admin_settings", "Settings"),
    ("admin_users.html", "admin_users", "User accounts"),
    ("admin_2fa.html", "admin_2fa", "Two-factor auth"),
    ("admin_2fa_enable.html", "admin_2fa_enable", "Two-factor auth · Set up"),
    ("admin_about.html", "admin_about", "About"),
    ("admin_clear_browser_cache.html", "admin_system_clear_browser_cache", "System · Clear browser cache"),
]

# Templates that are page furniture rather than a page, so they have no entry above and
# nothing to link to. Listed explicitly so the "is every template covered" test can tell
# a deliberate omission from a page somebody forgot to register.
NON_PAGE_TEMPLATES = {"admin_base.html"}

_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
_TAG = re.compile(r"<[^>]+>", re.S)
_LABEL = re.compile(r"<label\b[^>]*>(.*?)</label>", re.S | re.I)
_HINT = re.compile(r'<div\b[^>]*class="[^"]*field-hint[^"]*"[^>]*>(.*?)</div>', re.S | re.I)
_NAME = re.compile(r'\bname="([A-Za-z0-9_\[\]-]+)"')
_H2 = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.S | re.I)

MAX_RESULTS = 12
# A title longer than this is a hint that wandered into a label, not a label. Truncated
# rather than dropped: a long one is still findable, it just doesn't get to fill the
# result list.
MAX_TITLE_CHARS = 90

_cache = {"entries": None, "stamp": None}
_cache_lock = threading.Lock()


def clear_caches():
    """Drops the built index. Wired into the admin System page's "Clear cached data"
    button (app._clear_all_caches()), same as every other module-level cache here."""
    with _cache_lock:
        _cache["entries"] = None
        _cache["stamp"] = None


def cache_summary():
    entries = _cache["entries"]
    return {"admin_search_entries": len(entries) if entries is not None else 0}


def _plain(fragment):
    """Template fragment -> the text a person would actually see.

    Jinja tags go first, then HTML tags, then whitespace collapses. `{{ ... }}` becomes
    a space rather than nothing, so `{{ a }}{{ b }}` doesn't fuse two words together."""
    text = _JINJA.sub(" ", fragment)
    text = _TAG.sub(" ", text)
    return " ".join(html.unescape(text).split())


def _template_stamp():
    """The mtimes of every indexed template, so an edit invalidates the cache without a
    restart - the same trick asset_url() uses, and cheap for ~30 local stat() calls."""
    stamps = []
    for filename, _endpoint, _label in PAGES:
        path = os.path.join(TEMPLATE_DIR, filename)
        try:
            stamps.append(os.path.getmtime(path))
        except OSError:
            stamps.append(0)
    return tuple(stamps)


def _identifier_words(filename, endpoint):
    """Extra searchable words for a page, from its own template and endpoint names.

    Without this, "2fa" finds the four step-up code prompts scattered around the panel
    but not the Two-factor auth page itself, whose visible label contains no such
    string. Same for "seerr", "discord" and anything else whose file name is the term a
    person would actually type."""
    raw = filename.replace(".html", "") + " " + endpoint
    return " ".join(part for part in re.split(r"[_\-.]+", raw) if part and part != "admin")


def _entries_for_template(filename, endpoint, page_label):
    """Every searchable thing on one page.

    A single entry per *control*, keyed on the input's `name` where there is one - that
    name is also what the search route hands back as `jump`, so arriving on the page can
    scroll to and highlight the right control rather than dumping the admin at the top
    of a long page and leaving them to find it."""
    path = os.path.join(TEMPLATE_DIR, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as e:
        _logger.warning("Could not index admin template %s: %s", filename, e)
        return []

    entries = []
    seen_titles = set()
    identifiers = _identifier_words(filename, endpoint)

    def add(title, extra="", jump="", aliases=""):
        title = title.strip()
        if not title or len(title) < 2:
            return
        if len(title) > MAX_TITLE_CHARS:
            title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
        key = (title.lower(), jump)
        if key in seen_titles:
            return
        seen_titles.add(key)
        entries.append({
            "title": title,
            "page": page_label,
            "endpoint": endpoint,
            "jump": jump,
            # Other names for this destination, scored almost as strongly as the title.
            # Only the page-level entry carries any, and that is what makes "2fa" land
            # on the Two-factor auth page rather than on the four step-up code prompts
            # dotted around the panel, whose visible labels do contain "2FA" while the
            # page's own label ("Two-factor auth") does not.
            "aliases": aliases.lower(),
            # What actually gets matched: the visible title, the page name (so "settings
            # logo" works), the surrounding hint text (so "trickplay" finds the Jellyfin
            # tasks checkbox) and the field name (so a setting copied out of a log line
            # or .env finds itself without knowing its English label).
            "haystack": " ".join([title, page_label, extra, jump, identifiers]).lower(),
        })

    # Section headings, so a page is findable by what it's called even when the words
    # appear in no individual label.
    for heading in _H2.findall(source):
        add(_plain(heading), extra=page_label)

    # Each <label> is one control. The name is taken from the label's own inputs first
    # (a wrapping `<label class="field-check"><input ...>` - the checkbox-list pattern
    # this codebase uses everywhere), falling back to `for=`, which is how the separate
    # label-then-input fields are written.
    for match in _LABEL.finditer(source):
        block = match.group(0)
        title = _plain(match.group(1))
        names = _NAME.findall(block)
        jump = names[0] if names else ""
        if not jump:
            for_match = re.search(r'\bfor="([A-Za-z0-9_-]+)"', block)
            if for_match:
                jump = for_match.group(1)
        # The hint that follows this label, up to the next label, is part of what the
        # control means - and is where most of the searchable vocabulary lives.
        tail = source[match.end():match.end() + 900]
        next_label = tail.find("<label")
        if next_label != -1:
            tail = tail[:next_label]
        hint = " ".join(_plain(h) for h in _HINT.findall(tail))
        add(title, extra=hint, jump=jump)

    # The page itself, always - so every page is reachable by name even if it has no
    # controls at all (Reports and Resources are mostly tables).
    add(page_label, extra=page_label, aliases=identifiers)
    return entries


def build_index():
    """Every searchable entry across every admin page, cached until a template changes."""
    stamp = _template_stamp()
    with _cache_lock:
        if _cache["entries"] is not None and _cache["stamp"] == stamp:
            return _cache["entries"]

    entries = []
    for filename, endpoint, page_label in PAGES:
        entries.extend(_entries_for_template(filename, endpoint, page_label))

    with _cache_lock:
        _cache["entries"] = entries
        _cache["stamp"] = stamp
    return entries


def _score(entry, terms):
    """How well one entry matches, or None for "not at all".

    Every term has to appear somewhere - an AND, not an OR, because "disk alert" should
    narrow rather than widen. Ranking then prefers a title match over a match buried in
    a hint, and a whole-word match over a fragment, so searching "logs" puts the Logs
    page above every hint that happens to mention logging."""
    haystack = entry["haystack"]
    title = entry["title"].lower()
    aliases = entry.get("aliases", "")
    score = 0
    for term in terms:
        if term not in haystack:
            return None
        if title == term:
            score += 100
        elif re.search(r"\b" + re.escape(term) + r"\b", aliases):
            # A page found by its own file/endpoint name ("2fa", "seerr") is the
            # destination being asked for, not a passing mention of it.
            score += 60
        elif title.startswith(term):
            score += 40
        elif re.search(r"\b" + re.escape(term), title):
            score += 25
        elif term in title:
            score += 12
        else:
            score += 2
    # A control you can be taken directly to beats a bare page link for the same words.
    if entry["jump"]:
        score += 3
    # Shorter titles are more likely to be the thing itself rather than prose about it.
    score -= len(entry["title"]) / 200.0
    return score


def search(query, limit=MAX_RESULTS):
    """Ranked matches for `query`. An empty or one-character query matches nothing:
    every entry would match, which is a list, not an answer."""
    terms = [t for t in (query or "").lower().split() if t]
    if not terms or len(query.strip()) < 2:
        return []
    scored = []
    for entry in build_index():
        score = _score(entry, terms)
        if score is not None:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _score_value, entry in scored[:limit]]
