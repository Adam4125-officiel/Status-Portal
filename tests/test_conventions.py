"""Machine-checked versions of the rules in CLAUDE.md.

Why this file exists
--------------------
CLAUDE.md is over a thousand lines. Asking any assistant - and smaller/faster models
especially - to hold all of it in mind while editing one function is a losing bet, and
several of the rules in there exist *because* a previous session forgot one of the
others. Prose can only ask; a failing test tells.

So: every rule below is one that (a) has actually been broken at least once, and
(b) can be checked without running the app. A violation now shows up as a red test
naming the file and the fix, instead of as a bug the user finds on their server weeks
later.

Rules for adding to this file
-----------------------------
- **No false positives.** A check that fires on legitimate code is worse than no check,
  because the correct response becomes "ignore the convention tests". Encode the real
  exceptions (see the logo case in test_templates_use_asset_url) rather than loosening
  the rule until it never fires.
- Each test's failure message must say what to do, not just what's wrong. The reader is
  someone who has not read the matching CLAUDE.md section.
- Static checks only. Anything needing a running app belongs in test_app.py.
"""
import ast
import os
import re

import pytest

import app as app_module

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _template_files():
    root = os.path.join(REPO, "templates")
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            if name.endswith(".html"):
                path = os.path.join(dirpath, name)
                yield os.path.relpath(path, REPO), _read(path)


def _python_modules():
    for name in sorted(os.listdir(REPO)):
        if name.endswith(".py"):
            yield name, _read(name)


# ---------------------------------------------------------------------------
# The config split: env var vs DB setting, and "three files, not one"
# ---------------------------------------------------------------------------
def test_every_portal_env_var_is_documented_in_all_three_places():
    """CLAUDE.md: a new PORTAL_* env var means config.py, .env.example *and*
    docker-compose.yml. Compose only auto-loads .env for substitution into that file -
    it does not inject it into the container - so a variable missing from compose's
    environment: block is silently ignored under Docker no matter how correctly the
    user set it."""
    declared = set(re.findall(r'os\.environ\.get\("(PORTAL_[A-Z0-9_]+)"', _read("config.py")))
    documented = set(re.findall(r"^(PORTAL_[A-Z0-9_]+)=", _read(".env.example"), re.M))
    composed = set(re.findall(r"- (PORTAL_[A-Z0-9_]+)=", _read("docker-compose.yml")))

    assert declared, "no PORTAL_* variables found in config.py - did the parsing break?"
    assert not declared - documented, (
        f"Missing from .env.example: {sorted(declared - documented)}. "
        "Every PORTAL_* variable config.py reads must be documented there.")
    assert not declared - composed, (
        f"Missing from docker-compose.yml's environment: block: {sorted(declared - composed)}. "
        "Without it the setting is silently ignored under Docker.")


def test_only_config_reads_os_environ():
    """CLAUDE.md: nothing but config.py reads os.environ directly - that's what keeps
    every setting discoverable in one place, and what the .env.example/compose check
    above relies on to be complete."""
    offenders = [name for name, src in _python_modules()
                 if name != "config.py" and re.search(r"\bos\.environ\b", src)]
    assert not offenders, (
        f"{offenders} read os.environ directly. Add the setting to config.py and import "
        "it from there instead.")


# ---------------------------------------------------------------------------
# Static assets and caching
# ---------------------------------------------------------------------------
def test_templates_use_asset_url_for_css_and_js():
    """CLAUDE.md: every CSS/JS reference goes through asset_url(), never a bare
    url_for('static', ...). The documented update process extracts a release zip over
    the existing folder, which changes a file's contents but never its URL - without
    the ?v=<mtime> buster the browser keeps serving the previous release's copy. This
    has already silently shadowed a shipped fix once.

    Static references that are *not* CSS/JS (the uploaded logo) are allowed to use
    url_for directly as long as they carry their own version query - that's the
    site_logo_version mechanism, which does the same job."""
    offenders = []
    for path, src in _template_files():
        for match in re.finditer(r"url_for\(\s*'static'[^)]*\)", src):
            snippet = match.group(0)
            line = src[:match.start()].count("\n") + 1
            # The version query sits *after* Jinja's closing "}}", not immediately
            # after the url_for() call - e.g. "{{ url_for(...) }}?v={{ version }}".
            trailing = src[match.end():match.end() + 40]
            if re.search(r"filename='(css|js)/", snippet):
                offenders.append(f"{path}:{line} (CSS/JS must use asset_url())")
            elif not re.match(r"\s*\}\}\?v=", trailing):
                offenders.append(f"{path}:{line} (static reference with no ?v= cache-buster)")
    assert not offenders, (
        "Un-cache-busted static references:\n  " + "\n  ".join(offenders) +
        "\nUse asset_url('css/...') / asset_url('js/...') instead.")


def test_send_file_max_age_default_is_only_safe_with_busting():
    """A long browser cache lifetime is only safe because every asset URL is
    cache-busted. If this is ever raised or lowered, the reasoning above has to be
    revisited - this test exists to make that a deliberate edit rather than a silent
    one."""
    assert app_module.app.config["SEND_FILE_MAX_AGE_DEFAULT"].days == 30


# ---------------------------------------------------------------------------
# The incident lifecycle's level-triggered invariant
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("func_name, previous_arg", [
    ("_handle_incident_lifecycle", "previous_status"),
    ("_handle_integration_incident_lifecycle", "previous_reachable"),
])
def test_incident_open_branch_stays_level_triggered(func_name, previous_arg):
    """CLAUDE.md calls this the single easiest invariant in the codebase to "tidy"
    back into a bug, and it has already broken exactly that way once.

    The branch that *opens* an incident must test the new status alone - never
    `previous_status != "down"`. Idempotency already comes from the
    get_open_auto_incident_for_service() guard. An edge-trigger silently breaks for a
    service that was already down the last time this would have run (during a startup
    grace period, say): previous_status is already "down" by then, so no fresh
    transition is ever seen and the service can stay down forever with no incident.

    Checked on the AST rather than by grepping, so a reworded but equivalent
    edge-trigger still fails."""
    tree = ast.parse(_read("app.py"))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    assert func is not None, f"{func_name}() not found in app.py - was it renamed?"

    opening_ifs = [node for node in ast.walk(func)
                   if isinstance(node, ast.If)
                   and "create_auto_incident" in ast.dump(node.body and ast.Module(body=node.body, type_ignores=[]))]
    assert opening_ifs, f"no incident-opening branch found in {func_name}()"

    for node in opening_ifs:
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        assert previous_arg not in names, (
            f"{func_name}() opens an incident behind a condition referencing "
            f"'{previous_arg}'. That makes it edge-triggered, which is the bug. "
            "Open whenever the new status is down, full stop - the "
            "get_open_auto_incident_for_service() guard provides idempotency.")


# ---------------------------------------------------------------------------
# The 'slow' status tier
# ---------------------------------------------------------------------------
ALL_STATUSES = {"operational", "slow", "degraded", "maintenance", "down"}


@pytest.mark.parametrize("module, constant", [
    ("app", "STATUS_BADGE_LABEL"),
    ("app", "STATUS_BADGE_COLOR"),
    ("discord_bot", "PRESENCE_TEXT"),
    ("discord_bot", "_EMBED_COLOR_NAME"),
])
def test_status_maps_cover_every_status_including_slow(module, constant):
    """CLAUDE.md: 'slow' is a real fifth status, and any place the four original ones
    are enumerated needs an entry for it too. A map missing it doesn't crash - it
    renders a blank badge or a missing colour, which is exactly the kind of thing that
    ships unnoticed."""
    mod = __import__(module)
    mapping = getattr(mod, constant)
    assert set(mapping) == ALL_STATUSES, (
        f"{module}.{constant} covers {sorted(mapping)}; expected {sorted(ALL_STATUSES)}. "
        "Every status enumeration needs a 'slow' entry.")


# ---------------------------------------------------------------------------
# CSRF coverage
# ---------------------------------------------------------------------------
def test_every_post_route_is_csrf_covered_or_a_known_public_exception():
    """CLAUDE.md: CSRF protection is a before_request hook, not per-route wiring.
    That's what makes a new admin route protected for free - and it's also why a POST
    route added *outside* /admin/ silently gets no protection at all.

    The hook delegates to app._csrf_required_for(path, method), which covers
    everything under /admin/ plus an explicit set of public paths. /report is the one
    remaining deliberate exception, and only while nobody is signed in: its original
    justification was that it exercises no authenticated privilege, which stops being
    true once a report is attributable to a Jellyfin user, so it becomes protected
    exactly then (see report_problem() and _report_login_required()).

    Adding a second unprotected public POST route is a decision to make on purpose,
    which is what this test forces."""
    public_post_exceptions = {"/report"}
    unprotected = set()
    with app_module.app.test_request_context("/"):
        for rule in app_module.app.url_map.iter_rules():
            if "POST" not in rule.methods or rule.rule in public_post_exceptions:
                continue
            if not app_module._csrf_required_for(rule.rule, "POST"):
                unprotected.add(rule.rule)
    assert not unprotected, (
        f"POST routes with no CSRF protection: {sorted(unprotected)}. "
        "Either move the route under /admin/, add it to _CSRF_PROTECTED_PUBLIC_PATHS "
        "in app.py, or add it to public_post_exceptions here with its own anti-abuse "
        "measures (see report_problem()).")


def test_the_report_form_becomes_csrf_protected_once_a_user_is_signed_in():
    """The other half of the exception above: /report's exemption is conditional, and
    a change that made it unconditional again would silently leave an authenticated
    form cross-postable."""
    with app_module.app.test_request_context("/report", method="POST"):
        assert app_module._csrf_required_for("/report", "POST") is False
    with app_module.app.test_request_context("/report", method="POST") as ctx:
        ctx.session["portal_user"] = {"id": "u1", "name": "someone"}
        assert app_module._csrf_required_for("/report", "POST") is True


# ---------------------------------------------------------------------------
# Admin and visitor sessions must stay separate
# ---------------------------------------------------------------------------
def test_only_the_admin_session_helper_ever_sets_logged_in():
    """The structural guarantee behind "a Jellyfin user is never an admin": the
    `logged_in` session key is written in exactly one function. If a second place
    starts setting it, the isolation stops being structural and becomes something
    someone has to remember, which is how this class of bug happens."""
    tree = ast.parse(_read("app.py"))
    setters = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == "session"
                    and isinstance(target.slice, ast.Constant) and target.slice.value == "logged_in"):
                parent = next((f.name for f in ast.walk(tree)
                               if isinstance(f, ast.FunctionDef)
                               and any(n is node for n in ast.walk(f))), "<module>")
                setters.add(parent)
    assert setters == {"_start_admin_session"}, (
        f"session['logged_in'] is set in {sorted(setters)}; it must only ever be set in "
        "_start_admin_session(). The admin session and the Jellyfin visitor session are "
        "separate identities - see app.py's 'Visitor sessions' section.")


def test_the_visitor_session_helper_never_touches_admin_session_keys():
    """Same guarantee from the other direction."""
    tree = ast.parse(_read("app.py"))
    for name in ("_start_user_session", "_end_user_session"):
        func = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert func is not None, f"{name}() not found in app.py - was it renamed?"
        # Strip the docstring before inspecting: these functions *explain* the rule
        # in prose, and a check that fires on the comment describing it is exactly
        # the kind of false positive that teaches people to ignore this file.
        body = [n for n in func.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                        and isinstance(n.value.value, str))]
        code = ast.unparse(ast.Module(body=body, type_ignores=[]))
        for forbidden in ("logged_in", "awaiting_totp", "csrf_token"):
            assert forbidden not in code, (
                f"{name}() references the admin session key '{forbidden}'. The visitor "
                "session must only ever read and write its own keys.")
        assert "session.clear" not in code, (
            f"{name}() calls session.clear(), which would sign an admin out of the admin "
            "panel as a side effect of a visitor-side action. Pop the visitor keys instead.")


def test_admin_routes_are_all_gated_by_the_admin_decorator():
    """Every view under /admin/ must carry @login_required (which reads `logged_in`
    and nothing else), so no amount of getting the visitor flow wrong can expose an
    admin page. The two login pages are the deliberate exceptions - they're how you
    get a session in the first place."""
    tree = ast.parse(_read("app.py"))
    exempt = {"admin_login", "admin_logout"}
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        routes = [d for d in node.decorator_list
                  if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"
                  and d.args and isinstance(d.args[0], ast.Constant)
                  and str(d.args[0].value).startswith("/admin/")]
        if not routes or node.name in exempt:
            continue
        names = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
        if "login_required" not in names:
            missing.append(node.name)
    assert not missing, (
        f"Admin routes with no @login_required: {missing}. Every /admin/ view needs it - "
        "it is what keeps a signed-in Jellyfin visitor out of the admin panel.")


# ---------------------------------------------------------------------------
# Step-up 2FA on the destructive actions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("route_func", [
    "admin_host_control", "admin_system_restart", "admin_update",
])
def test_destructive_routes_go_through_require_totp(route_func):
    """CLAUDE.md: these three are the actions where a stolen/replayed session cookie
    alone must not be enough, and they must call the shared _require_totp() helper
    rather than each hand-rolling the check - three copies is three chances for one to
    quietly stop matching the others."""
    tree = ast.parse(_read("app.py"))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == route_func), None)
    assert func is not None, f"{route_func}() not found in app.py - was it renamed?"
    called = {n.func.id for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_require_totp" in called, (
        f"{route_func}() no longer calls _require_totp(). Destructive actions need "
        "step-up re-authentication via that shared helper.")


# ---------------------------------------------------------------------------
# Public page sections
# ---------------------------------------------------------------------------
def test_every_public_section_has_a_template():
    """CLAUDE.md: index() includes 'sections/<key>.html' for each configured key with
    no existence check, so a key in PUBLIC_SECTIONS without a matching file is a
    TemplateNotFound on the public page - for every visitor, not just the admin."""
    missing = [key for key, _ in app_module.PUBLIC_SECTIONS
               if not os.path.isfile(os.path.join(REPO, "templates", "sections", f"{key}.html"))]
    assert not missing, (
        f"PUBLIC_SECTIONS keys with no templates/sections/<key>.html: {missing}")


def test_reorderable_sections_are_not_underscore_prefixed():
    """CLAUDE.md's partial-naming rule: templates/sections/_*.html is an AJAX fragment
    or a per-item partial, not a reorderable block. A reorderable key starting with an
    underscore would mean the two naming schemes had collided."""
    bad = [key for key, _ in app_module.PUBLIC_SECTIONS if key.startswith("_")]
    assert not bad, f"Reorderable section keys must not start with '_': {bad}"


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------
def test_no_native_multi_selects_in_templates():
    """CLAUDE.md: use a checkbox list, never <select multiple>, for any service/entity
    picker. A <select multiple> here shipped a real bug where an admin submitted
    "everything except the service they picked" - request.form.getlist() reads
    identically from repeated checkboxes, so this costs no route changes."""
    offenders = [f"{path}:{src[:m.start()].count(chr(10)) + 1}"
                 for path, src in _template_files()
                 for m in re.finditer(r"<select[^>]*\bmultiple\b", src)]
    assert not offenders, (
        "<select multiple> found at:\n  " + "\n  ".join(offenders) +
        "\nUse the shared .checkbox-list/.field-check styles instead.")


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------
def test_every_module_level_cache_is_reset_between_tests():
    """CLAUDE.md: a new module-level cache needs resetting in tests/conftest.py or it
    leaks across tests - and a leaked cache doesn't fail where it was left behind, it
    makes some later test read stale data and pass or fail for the wrong reason.

    conftest calls each module's own clear_caches() where one exists, so a cache added
    *inside* one of those helpers is already covered; this catches a new top-level
    global that nothing clears."""
    conftest = _read("tests", "conftest.py")
    cleared_by_helper = {
        "monitoring.py": _read("monitoring.py").split("def clear_caches")[-1].split("\ndef ")[0],
        "integrations.py": _read("integrations.py").split("def clear_caches")[-1].split("\ndef ")[0],
        "scheduler.py": _read("scheduler.py").split("def clear_caches")[-1].split("\ndef ")[0],
        "updater.py": _read("updater.py").split("def clear_update_cache")[-1].split("\ndef ")[0],
    }
    missing = []
    for name, src in _python_modules():
        for global_name in re.findall(r"^(_[a-z0-9_]*(?:cache|state|salt)[a-z0-9_]*)\s*=\s*[\{\[]",
                                       src, re.M):
            if global_name in conftest:
                continue
            if global_name in cleared_by_helper.get(name, ""):
                continue
            missing.append(f"{name}:{global_name}")
    assert not missing, (
        f"Module-level caches not reset between tests: {missing}. Add them to "
        "_reset_module_state() in tests/conftest.py, or to that module's own "
        "clear_caches() helper (which conftest already calls).")


# ---------------------------------------------------------------------------
# Recurring work goes through the scheduler, not a fourth background thread
# ---------------------------------------------------------------------------
def test_no_new_bare_background_loops():
    """CLAUDE.md: a new recurring job is a scheduler.register() call, not another
    `while True` thread. Three such loops predate the framework and are allowed by
    name below - each is also registered via scheduler.register_loop() so it still
    shows up on /admin/tasks. A fourth means either making it a task, or (if it
    genuinely can't be one) registering it as a loop and adding it here on purpose."""
    allowed = {
        # The core health-check cycle. Not a task: switching it off from a browser
        # would also switch off incident detection.
        "app.py": 1,
        # Resource polling, which runs faster than SCHEDULER_TICK_SECONDS.
        "monitoring.py": 1,
        # The scheduler's own loop, which is what drives every task.
        "scheduler.py": 1,
    }
    offenders = []
    for name, src in _python_modules():
        found = len(re.findall(r"^\s*while True:", src, re.M))
        if found > allowed.get(name, 0):
            offenders.append(f"{name} ({found} found, {allowed.get(name, 0)} expected)")
    assert not offenders, (
        f"New bare background loop(s): {offenders}. Recurring work belongs in "
        "scheduler.register() - you get an on/off switch, a schedule, a last-run "
        "record and a 'Run now' button for free, and it shows up on /admin/tasks. "
        "If it genuinely cannot be a task (too frequent for the tick, or it lives in "
        "another event loop), register it with scheduler.register_loop() so it is at "
        "least visible, and add it to `allowed` here with the reason.")
