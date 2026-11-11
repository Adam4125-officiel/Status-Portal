"""Admin panel on a phone: collapsible menu, readable tables, no inline onchange."""
import re
from pathlib import Path

import db

ROOT = Path(__file__).resolve().parent.parent


def _login(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})


def test_admin_menu_is_a_details_with_current_page_and_badge_total(client):
    db.create_problem_report("Broken")
    _login(client)
    html = client.get("/admin/services").get_data(as_text=True)
    assert 'id="admin_nav_menu"' in html and "<summary" in html
    summary = re.search(r"<summary.*?</summary>", html, re.DOTALL).group(0)
    assert "Services" in summary
    assert 'class="nav-badge">1<' in summary
    assert "js/admin_nav.js" in html


def test_menu_summary_names_other_pages_too(client):
    _login(client)
    html = client.get("/admin/logs").get_data(as_text=True)
    assert "Logs" in re.search(r"<summary.*?</summary>", html, re.DOTALL).group(0)


def test_services_table_marks_secondary_and_short_columns(client):
    db.create_service({"name": "Plex", "url": "http://plex.example"})
    _login(client)
    html = client.get("/admin/services").get_data(as_text=True)
    head = re.search(r"<thead>.*?</thead>", html, re.DOTALL).group(0)
    assert head.count("col-secondary") == 3
    assert head.count("col-short") == 2
    css = (ROOT / "static/css/style.css").read_text()
    assert ".admin-table .col-secondary { display: none; }" in css
    assert ".admin-table .col-short { white-space: nowrap;" in css


def test_reports_status_select_has_no_inline_handler(client):
    db.create_problem_report("Broken")
    _login(client)
    html = client.get("/admin/reports").get_data(as_text=True)
    assert "onchange" not in html
    assert "data-autosubmit" in html
    assert "js/admin_autosubmit.js" in html
    js = (ROOT / "static/js/admin_autosubmit.js").read_text()
    assert "requestSubmit" in js
