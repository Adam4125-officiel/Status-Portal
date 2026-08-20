#!/usr/bin/env bash
# Stop hook: surfaces violations of the project rules that tests/test_conventions.py
# encodes, at the moment a turn finishes, without anyone having to remember to run it.
#
# Why a hook and not just a line in CLAUDE.md: the whole problem this addresses is a
# rule being forgotten, and "remember to run the test that catches what you forgot"
# has exactly the same failure mode as the rule itself.
#
# Silent on success. On failure it asks the model to fix the violation *once* - and if
# the model was already woken by this same hook (stop_hook_active), it downgrades to a
# message for the human instead of asking again, so a violation it can't fix can never
# turn into a loop.
set -u

input=$(cat 2>/dev/null || echo '{}')

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/..}" 2>/dev/null || exit 0
# Not the portal repo (or the file is gone): do nothing at all rather than guess.
[ -f tests/test_conventions.py ] || exit 0

output=$(python -m pytest tests/test_conventions.py -q 2>&1) && exit 0

python3 - "$output" "$input" <<'PY'
import json
import sys

output, raw_input = sys.argv[1], sys.argv[2]
try:
    already_woken = bool(json.loads(raw_input).get("stop_hook_active"))
except (ValueError, AttributeError):
    already_woken = False

failed = [line.split("::", 1)[-1].strip()
          for line in output.splitlines() if line.startswith("FAILED")]
listing = "\n".join(f"  - {name}" for name in failed) or "  (see the full output)"
detail = (f"tests/test_conventions.py is failing:\n{listing}\n\n"
          "These are this project's own conventions, enforced as tests because they "
          "have each been broken before - see the failure message and the matching "
          "section of CLAUDE.md. Run: python -m pytest tests/test_conventions.py -q")

if already_woken:
    # Second time round: stop asking, tell the human instead.
    print(json.dumps({"systemMessage": detail}))
else:
    print(json.dumps({"decision": "block", "reason": detail}))
PY
exit 0
