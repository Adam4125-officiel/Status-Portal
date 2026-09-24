import time

import pyotp

import db
import twofactor


def test_generate_secret_is_valid_base32():
    secret = twofactor.generate_secret()
    assert len(secret) >= 16
    pyotp.TOTP(secret).now()  # must not raise


def test_verify_code_accepts_current_code_and_rejects_wrong_one():
    secret = twofactor.generate_secret()
    current = pyotp.TOTP(secret).now()
    assert twofactor.verify_code(secret, current) is True
    assert twofactor.verify_code(secret, "000000") is False


def test_verify_code_handles_missing_input_gracefully():
    secret = twofactor.generate_secret()
    assert twofactor.verify_code(secret, "") is False
    assert twofactor.verify_code(secret, None) is False
    assert twofactor.verify_code("", "123456") is False
    assert twofactor.verify_code(None, "123456") is False


def test_verify_code_rejects_garbage_input_without_raising():
    secret = twofactor.generate_secret()
    assert twofactor.verify_code(secret, "not-a-code") is False


def test_qr_code_svg_produces_svg_markup():
    uri = twofactor.provisioning_uri(twofactor.generate_secret(), account_name="admin")
    svg = twofactor.qr_code_svg(uri)
    assert svg.strip().startswith("<?xml") or "<svg" in svg
    assert "<svg" in svg


def test_is_enabled_requires_both_flag_and_secret(isolated_db):
    assert twofactor.is_enabled() is False

    db.set_setting("admin_totp_enabled", "1")
    assert twofactor.is_enabled() is False  # no secret yet - safe fallback

    db.set_setting("admin_totp_secret", twofactor.generate_secret())
    assert twofactor.is_enabled() is True

    db.set_setting("admin_totp_enabled", "0")
    assert twofactor.is_enabled() is False


def test_reset_flag_file_disables_2fa_and_is_self_cleaning(isolated_db, monkeypatch, tmp_path):
    flag_path = tmp_path / "RESET_2FA"
    monkeypatch.setattr(twofactor, "RESET_FLAG_PATH", str(flag_path))

    db.set_setting("admin_totp_enabled", "1")
    db.set_setting("admin_totp_secret", twofactor.generate_secret())
    assert twofactor.is_enabled() is True

    assert twofactor.check_and_process_reset_flag() is False  # no file yet - no-op
    assert twofactor.is_enabled() is True

    flag_path.write_text("")
    assert twofactor.check_and_process_reset_flag() is True
    assert twofactor.is_enabled() is False
    assert db.get_setting("admin_totp_secret") == ""
    assert not flag_path.exists()  # self-cleaning

    # Idempotent - calling again with the file already gone is a clean no-op.
    assert twofactor.check_and_process_reset_flag() is False


def test_verify_and_consume_accepts_each_code_only_once(isolated_db):
    """A phished code must not be replayable while valid_window keeps it valid."""
    secret = twofactor.generate_secret()
    code = pyotp.TOTP(secret).now()
    assert twofactor.verify_and_consume(secret, code) is True
    assert twofactor.verify_and_consume(secret, code) is False
    assert twofactor.verify_and_consume(secret, "000000") is False


def test_verify_and_consume_refuses_an_earlier_step_after_a_later_one(isolated_db):
    """RFC 6238 5.2: once a step has been accepted, nothing at or before it is. The
    next step's code (inside valid_window) still works."""
    secret = twofactor.generate_secret()
    totp = pyotp.TOTP(secret)
    current, following = totp.now(), totp.at(time.time() + 30)
    assert twofactor.verify_and_consume(secret, following) is True
    assert twofactor.verify_and_consume(secret, current) is False


def test_verify_and_consume_handles_missing_and_garbage_input(isolated_db):
    secret = twofactor.generate_secret()
    for code in ("", None, "not-a-code"):
        assert twofactor.verify_and_consume(secret, code) is False
    assert twofactor.verify_and_consume("", "123456") is False
    # Nothing was accepted, so nothing was recorded.
    assert db.get_setting(twofactor.LAST_STEP_SETTING) is None


def test_disable_forgets_the_last_accepted_step(isolated_db):
    """Otherwise re-enrolling straight after disabling would refuse the new secret's
    code for sharing a time step with the code that disabled the old one."""
    old, new = twofactor.generate_secret(), twofactor.generate_secret()
    db.set_setting("admin_totp_enabled", "1")
    db.set_setting("admin_totp_secret", old)
    assert twofactor.verify_and_consume(old, pyotp.TOTP(old).now()) is True
    twofactor.disable()
    assert twofactor.is_enabled() is False
    assert db.get_setting(twofactor.LAST_STEP_SETTING) == ""
    assert twofactor.verify_and_consume(new, pyotp.TOTP(new).now()) is True
