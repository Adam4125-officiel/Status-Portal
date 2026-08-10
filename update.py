"""
update.py — Update this portal to the latest release from GitHub, from a shell.

This is a thin CLI wrapper: every bit of actual logic lives in updater.py, which
the admin panel's "Update now" button calls too, so the two can never drift apart.

It deliberately does not import app.py or Flask - it is the tool you reach for
precisely when the web UI is broken, so it must work on a portal that won't start.

    python update.py                      # what version am I on, is there a new one
    python update.py check
    python update.py apply                # update (asks for confirmation first)
    python update.py apply --yes          # ...without asking
    python update.py apply --channel unstable
    python update.py rollback             # undo the last update
    python update.py list-backups
    python update.py channel unstable     # change the stored channel preference

After `apply`, restart the portal yourself - this script has no way to know how
you launch it (systemd, Task Scheduler, a terminal), so it won't guess.

NEVER OVERWRITTEN by an update, whatever else changes: instance/ (your database,
your logs, your backups), .env, and static/uploads/ (your logo).
"""
import argparse
import json
import os
import shutil
import sys

# Mirrors updater.CHANNELS. Duplicated as a literal purely so argparse can be built
# without importing the app - see _load_updater() below. A test asserts the two
# stay identical.
CHANNELS = ("stable", "unstable")

APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_updater():
    """Imports updater.py, or exits with an explanation.

    Deliberately lazy rather than a module-level import: an update that goes wrong
    can leave a tree whose own modules no longer import (a half-applied set of
    files, a config.py from a release that predates something updater.py needs).
    That is precisely when someone runs this script - so it must get far enough to
    reach the emergency rollback below instead of dying on an ImportError."""
    try:
        import updater
        return updater
    except Exception as e:
        print(f"ERROR: this installation's own code won't import ({e}).\n"
              "       If you just applied an update, undo it with:\n"
              "           python update.py rollback --emergency", file=sys.stderr)
        sys.exit(2)


def _emergency_rollback(backup_name=None):
    """Last-resort rollback that imports nothing from this app.

    This is NOT a second update implementation - it only restores a backup that
    updater.py already created, by reading the manifest.json it already wrote. It
    exists because the normal path (updater.rollback) needs updater.py to import,
    and the one failure mode a rollback tool most needs to survive is "the update
    broke this app's code". Everything it does could equally be done by hand with
    `cp -r instance/update_backups/<name>/* .` - it just does it correctly,
    including removing files the update added."""
    backup_root = os.path.join(APP_ROOT, "instance", "update_backups")
    if not os.path.isdir(backup_root):
        print(f"No backups directory at {backup_root}.", file=sys.stderr)
        return 1
    names = sorted(d for d in os.listdir(backup_root)
                   if os.path.isfile(os.path.join(backup_root, d, "manifest.json")))
    if not names:
        print("No update backups found - nothing to roll back to.", file=sys.stderr)
        return 1
    if not backup_name:
        marker = os.path.join(APP_ROOT, "instance", "update_pending.json")
        try:
            with open(marker, "r", encoding="utf-8") as f:
                backup_name = json.load(f).get("backup")
        except (OSError, ValueError):
            backup_name = None
        if backup_name not in names:
            backup_name = names[-1]

    backup_dir = os.path.join(backup_root, backup_name)
    with open(os.path.join(backup_dir, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"EMERGENCY ROLLBACK to {manifest.get('from_version')} (backup {backup_name})")
    restored = removed = 0
    for name in manifest.get("replaced", []):
        source = os.path.join(backup_dir, name)
        if not os.path.isfile(source):
            print(f"  ! missing from backup, skipping: {name}")
            continue
        destination = os.path.join(APP_ROOT, name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        restored += 1
    for name in manifest.get("added", []):
        try:
            os.remove(os.path.join(APP_ROOT, name))
            removed += 1
        except OSError:
            pass
    for leftover in ("update_pending.json",):
        try:
            os.remove(os.path.join(APP_ROOT, "instance", leftover))
        except OSError:
            pass
    print(f"Restored {restored} file(s), removed {removed} added file(s).")
    print("Restart the portal.")
    return 0


def _print_check(result):
    print(f"Running:  {result['current_display']}")
    print(f"Channel:  {result['channel']}")
    if not result["ok"]:
        print(f"Latest:   couldn't check - {result['error']}")
        return 1
    print(f"Latest:   {result['latest']}" + ("  (prerelease)" if result["prerelease"] else ""))
    if result["update_available"]:
        print(f"\n>> An update is available: {result['current']} -> {result['latest']}")
        print(f"   Release notes: {result['latest_url']}")
        print("   Install it with: python update.py apply")
    elif result["ahead"]:
        print(f"\n== You are running {result['current']}, which is newer than the latest "
              f"{result['channel']} release. Nothing to do.")
    else:
        print("\n== Up to date.")
    return 0


def _confirm(prompt):
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def cmd_check(args):
    updater = _load_updater()
    return _print_check(updater.check_for_update(args.channel))


def cmd_apply(args):
    updater = _load_updater()
    result = updater.check_for_update(args.channel)
    if _print_check(result) == 1 and not args.force:
        return 1
    if not result["update_available"] and not args.force:
        return 0
    print()
    if not args.yes and not _confirm(
            f"Update this portal to {result['latest']}? Your database, .env and uploads are never touched."):
        print("Cancelled.")
        return 1
    print()
    try:
        outcome = updater.perform_update(
            channel=args.channel,
            force=args.force,
            install_deps=not args.no_deps,
            allow_dev_checkout=args.allow_dev_checkout,
        )
    except updater.UpdateError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    if outcome["applied"]:
        print(f"\nDone. Restart the portal now (e.g. `python serve_waitress.py`, or restart "
              f"your service/scheduled task).")
        print(f"If the new version doesn't start, undo it with: python update.py rollback")
    return 0


def cmd_rollback(args):
    if args.emergency:
        return _emergency_rollback(args.to)
    updater = _load_updater()
    backups = updater.list_backups()
    if not backups:
        print("No update backups found - nothing to roll back to.", file=sys.stderr)
        return 1
    marker = updater.read_pending_marker()
    target = args.to or (marker or {}).get("backup") or backups[-1]["name"]
    chosen = next((b for b in backups if b["name"] == target), None)
    if not chosen:
        print(f"No backup named '{target}'.", file=sys.stderr)
        return 1
    print(f"This will restore version {chosen['from_version']} "
          f"(undoing the update to {chosen['to_version']}).")
    if not args.yes and not _confirm("Roll back?"):
        print("Cancelled.")
        return 1
    try:
        updater.rollback(chosen["name"])
    except updater.UpdateError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_list_backups(args):
    updater = _load_updater()
    backups = updater.list_backups()
    if not backups:
        print("No update backups yet.")
        return 0
    print(f"{'BACKUP':<40} {'FROM':<12} {'TO':<12} FILES")
    for b in backups:
        print(f"{b['name']:<40} {b.get('from_version', '?'):<12} {b.get('to_version', '?'):<12} "
              f"{len(b.get('replaced', []))} replaced, {len(b.get('added', []))} added")
    print("\nRoll back to one with: python update.py rollback --to <BACKUP>")
    return 0


def cmd_channel(args):
    updater = _load_updater()
    if not args.channel_name:
        print(f"Current channel: {updater.get_channel()}")
        print(f"Available: {', '.join(updater.CHANNELS)}")
        print("  stable   - final releases only (recommended)")
        print("  unstable - also offers -rc.N prereleases, which are not yet user-verified")
        return 0
    try:
        updater.set_channel(args.channel_name)
    except updater.UpdateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Update channel set to '{args.channel_name}'.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="update.py",
        description="Update status-portal to the latest release from its GitHub "
                    "repository (that URL is fixed in updater.py and cannot be changed).")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="show the running and latest versions")
    check.add_argument("--channel", choices=CHANNELS, default=None,
                       help="override the stored channel for this run")
    check.set_defaults(func=cmd_check)

    apply_cmd = subparsers.add_parser("apply", help="download and install the latest release")
    apply_cmd.add_argument("--channel", choices=CHANNELS, default=None,
                           help="override the stored channel for this run")
    apply_cmd.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    apply_cmd.add_argument("--force", action="store_true",
                           help="reinstall even if already up to date")
    apply_cmd.add_argument("--no-deps", action="store_true",
                           help="skip `pip install -r requirements.txt` even if it changed")
    apply_cmd.add_argument("--allow-dev-checkout", action="store_true",
                           help="allow updating over a git working tree (this overwrites "
                                "tracked files - use `git pull` instead unless you're sure)")
    apply_cmd.set_defaults(func=cmd_apply)

    rollback_cmd = subparsers.add_parser("rollback", help="restore the files an update replaced")
    rollback_cmd.add_argument("--to", default=None, help="a specific backup name (see list-backups)")
    rollback_cmd.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    rollback_cmd.add_argument("--emergency", action="store_true",
                              help="restore a backup without importing this app's code - use "
                                   "this when an update left the portal unable to start")
    rollback_cmd.set_defaults(func=cmd_rollback)

    backups_cmd = subparsers.add_parser("list-backups", help="show available rollback points")
    backups_cmd.set_defaults(func=cmd_list_backups)

    channel_cmd = subparsers.add_parser("channel", help="show or set the update channel")
    channel_cmd.add_argument("channel_name", nargs="?", choices=CHANNELS, default=None)
    channel_cmd.set_defaults(func=cmd_channel)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Bare `python update.py` is the thing you type when you just want to know
        # where you stand, so it reads rather than writes.
        args = parser.parse_args(["check"])
    print(f"status-portal updater — app directory: {APP_ROOT}\n", flush=True)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
