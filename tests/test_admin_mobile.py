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
    summary = re.search(r"<summary.*?</summary>", html, re.S).group(0)
    assert "Services" in summary
    assert 'class="nav-badge">1<' in summary
    assert "js/admin_nav.js" in html


def test_menu_summary_names_other_pages_too(client):
    _login(client)
    html = client.get("/admin/logs").get_data(as_text=True)
    assert "Logs" in re.search(r"<summary.*?</summary>", html, re.S).group(0)