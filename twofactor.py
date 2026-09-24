"""
twofactor.py — Optional TOTP-based two-factor authentication for the admin login,
compatible with any standard authenticator app (Google Authenticator, Authy,
1Password, etc.). Off by default - the single admin can turn it on from
/admin/2fa; strongly recommended in the UI but never required, since some
people won't want it.

The QR code is generated fully server-side as an SVG via `qrcode`'s SVG image
factory - no Pillow/image-library dependency needed, and enrollment never makes
an external call (no third-party QR-image service), consistent with the rest of
this app staying self-contained. `pyotp`/`qrcode` are required dependencies (see
requirements.txt) - both are small, pure-Python packages, not lazy-imported
optional ones like nvidia-ml-py/discord.py, since 2FA is a core, always-available
feature rather than a rare integration.
"""
import datetime
import io
import logging
import os
import threading

import pyotp
import qrcode
import qrcode.image.svg

import db

_logger = logging.getLogger(__name__)

ISSUER_NAME = "Status Portal"

# A host-level (not web-admin) way to recover from a lost/broken authenticator
# device: create an empty file at this exact path and 2FA is disabled the next
# time anyone loads the login page - deliberately NOT reachable purely over the
# web (no "reset" button, no emergency backup code), since that would just be
# another secret to protect. Actual filesystem access to the host is a different,
# stronger trust boundary than knowing the admin password or holding a stolen
# session cookie.
RESET_FLAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "RESET_2FA")

# The last TOTP time step (30s counter) a code was accepted for. A code is only good
# once: without this, a code phished from the admin could be replayed for as long as
# valid_window keeps it valid (up to ~90s). Persisted rather than held in memory so
# a restart can't reopen that window. Cleared by disable(), so re-enrolling straight
# after disabling isn't refused for reusing the step the disable code consumed.
LAST_STEP_SETTING = "admin_totp_last_step"
# Read-compare-write on one settings row: two requests carrying the same code at the
# same moment must not both see it as unused.
_consume_lock = threading.Lock()


def is_enabled():
    """False whenever the secret is missing, even if the enabled flag is somehow
    still set - a safe fallback (password-only login) rather than a broken state
    that can never produce a valid code."""
    return db.get_setting("admin_totp_enabled", "0") == "1" and bool(db.get_setting("admin_totp_secret"))


def generate_secret():
    return pyotp.random_base32()


def provisioning_uri(secret, account_name):
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=ISSUER_NAME)


def verify_code(secret, code):
    """valid_window=1 tolerates the code from one 30s step before/after the
    current one - the standard TOTP allowance for clock drift and the time it
    takes a human to type 6 digits."""
    if not secret or not code:
        return False
    try:
        return pyotp.totp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


def _accepted_step(secret, code):
    """The time step `code` is valid for under the same valid_window=1 rule as
    verify_code(), or None. pyotp's verify() only answers yes/no, and the replay
    guard needs to know *which* step matched."""
    if not secret or not code:
        return None
    try:
        totp = pyotp.totp.TOTP(secret)
        code = str(code).strip()
        current = totp.timecode(datetime.datetime.now())
        for step in (current - 1, current, current + 1):
            if pyotp.utils.strings_equal(code, totp.generate_otp(step)):
                return step
    except Exception:
        return None
    return None


def verify_and_consume(secret, code):
    """verify_code(), plus each time step being accepted at most once. Use this for
    every check that grants something (login, step-up, enabling, disabling) -
    verify_code() on its own would accept the same code again until it expires.

    Any step at or before the last accepted one is refused, not just an exact
    repeat, as RFC 6238 section 5.2 recommends."""
    step = _accepted_step(secret, code)
    if step is None:
        return False
    with _consume_lock:
        last = db.parse_int(db.get_setting(LAST_STEP_SETTING, ""))
        if last is not None and step <= last:
            return False
        db.set_setting(LAST_STEP_SETTING, str(step))
    return True


def disable():
    """Turns 2FA off and forgets the secret and the replay guard's last step. Used
    by both the admin page and the host-level reset flag, so the two can't drift."""
    db.set_setting("admin_totp_enabled", "0")
    db.set_setting("admin_totp_secret", "")
    db.set_setting(LAST_STEP_SETTING, "")


def qr_code_svg(uri):
    """Renders the otpauth:// URI as an inline SVG QR code."""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def check_and_process_reset_flag():
    """Called on every /admin/login hit (see app.py) - cheap (a single
    os.path.exists() check) and takes effect immediately, no app restart needed.
    Self-cleaning: the flag file is deleted once processed, so this is a one-shot
    action, not a standing backdoor left enabled by accident."""
    if not os.path.exists(RESET_FLAG_PATH):
        return False
    disable()
    try:
        os.remove(RESET_FLAG_PATH)
    except OSError:
        _logger.exception("2FA reset flag file found but could not be removed")
    _logger.warning("Two-factor authentication reset via host-level RESET_2FA flag file")
    return True
