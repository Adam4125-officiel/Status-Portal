## Crash logging (`logging_setup.py`)

- `logging_setup.init_logging()` configures Python's standard `logging` module
  (rotating file under `instance/logs/app.log`, same gitignored directory as the DB,
  plus the console) — called once from `app.py`/`serve_waitress.py`'s `__main__`
  blocks, **never at plain import time**, specifically so importing these modules
  under pytest doesn't create log files as a side effect. If you add a third entry
  point, call it there too, in the same spot (before `db.init_db()`).
- Every background-thread error path goes through `logging.getLogger(__name__)` —
  `.exception(...)` inside an `except` block (auto-captures the traceback),
  `.info()`/`.warning()` for non-error diagnostics. Follow this pattern for any new
  diagnostic rather than reintroducing bare `print()`.
- `threading.excepthook` is set to `logging_setup._log_thread_exception` — the safety
  net for a background thread dying from something *outside* its own try/except.
  Without it, a thread dying that way is completely silent: "services just stopped
  updating" with zero trace anywhere.
- **Tests that assert on logged output must use pytest's `caplog` fixture, not
  `capsys`** — `capsys` only captures direct `print()`/stdout writes, and with no
  logging configured during tests a bare `logging.Logger` call goes to *stderr* via
  Python's handler of last resort, which `capsys.readouterr().out` won't see either.
  See `tests/test_monitoring.py`'s `test_vm_snapshot_logs_stderr_on_failure` for the
  pattern.

## Reading the logs from the admin panel (`/admin/logs`, `logging_setup.py`)

- **The readers live in `logging_setup.py`, not `app.py`.** That module already owns
  where the log files are and how they're named (`LOG_BASENAME`, `MAX_BACKUPS`,
  matching `RotatingFileHandler`'s own `app.log`, `app.log.1`, … convention), and
  that is exactly what a reader needs to know. It also keeps them testable with no
  Flask app in the picture.
- **`log_files()` filters the directory through one strict pattern (`_LOG_NAME`), and
  a download resolves by *membership in that list*.** The name a caller sends is never
  joined onto a directory, which is what makes path traversal structurally impossible
  here rather than something filtered out. Keep that shape if you add another log
  file — widen the pattern, don't start trusting the input.
- **Rotation is daily (`TimedRotatingFileHandler`, `when="midnight"`), keeping
  `config.LOG_RETENTION_DAYS` (`PORTAL_LOG_RETENTION_DAYS`, default 7) files.** It was
  size-based (2 MB × 3), which on a quiet portal is *months* of history — so the log
  page opened on entries from weeks ago, which is what prompted the change. A day is a
  unit a person reasons in, and it makes the retention setting mean something obvious.
  **Rotating on every start is the tempting alternative and is worse**: this app
  restarts itself (the updater, `/admin/system`, a systemd restart), so a
  crash-restart loop would blow through every retained file and delete exactly the
  history explaining the crash. `init_logging()` logs a "portal starting" banner
  instead, so a run is findable inside a file that spans several of them.
  The old `app.log.1`…`.3` files stay listed and downloadable on an upgraded install
  — they stop being written, but hiding them would strand history someone may want.
- **One day's file is capped at `MAX_BYTES_PER_FILE` (20 MB)** by
  `_CappedDailyFileHandler`: past it, one marker entry and then nothing until
  midnight's rotation. That bounds the disk to roughly the cap times the retention
  days, even under a flood of tracebacks. It drops rather than rotating early because
  a second file for the same day would collide with the date-named backups the
  retention count and `_LOG_NAME` rely on. It subclasses `TimedRotatingFileHandler`,
  so anything checking for that type still finds it.
- **Every read is bounded, which is why this is allowed in a request handler at
  all.** `read_tail()` seeks to the last `TAIL_MAX_BYTES` rather than reading a 2 MB
  file, and the page's `limit` is validated against a fixed tuple (`LOG_PAGE_SIZES`)
  so a query string can't ask the server for arbitrarily much work. Same class of
  cheap local file work as `updater.list_backups()`. **Don't add a cache** — a log
  page whose whole job is "what just happened" must not show a minute-old snapshot.
- **The unit of display is an *entry*, not a line.** `parse_entries()` treats a line
  matching the formatter's `timestamp LEVEL [name]` prefix as a new entry and
  everything else as a continuation of the one above. Filtering line-by-line would
  detach a traceback from the ERROR that produced it and then drop it — which is
  precisely what someone opened the page to read.
- **Terminal colour codes are stripped for display only, never from the file or a
  download.** Some libraries colour their own console output without knowing a file
  handler is also listening: werkzeug's development-server warning really does land
  in `app.log` as `\x1b[31m\x1b[1mWARNING…`, which a browser renders as gibberish
  mid-line. Found by reading the real file during a live smoke test, not by a test.
- **"Download full log" concatenates every rotated file, oldest first**, streamed via
  a generator — with the default rotation that's up to 8 MB, and a route has no
  reason to hold it in memory. Per-file downloads exist too, for when only one is
  wanted. Both pass `max_age=0`/`no-store` for the same reason the DB backup does:
  `SEND_FILE_MAX_AGE_DEFAULT` is 30 days and these are point-in-time files, not
  versioned assets.
- **Not behind `_require_totp()`, deliberately** — nothing here writes and nothing
  goes offline, which is the line that helper is scoped to. It is admin-only via
  `login_required` like everything else under `/admin/`, which matters, because the
  log quotes paths, user names and error text from every integration.
- **The view is live, by polling `/admin/logs/tail` for an HTML fragment** — same
  convention as `/api/incidents/more` (server-rendered fragment, not JSON), and the
  *same partial* the full page renders, so a live-appended entry cannot look
  different from one that was there on load. **Don't replace this with SSE or a
  WebSocket**: either holds a request thread per open page for as long as it stays
  open, and waitress runs a fixed pool (`config.WAITRESS_THREADS`) — a couple of
  forgotten admin tabs would eat the pool that serves the actual portal. The poll
  hands back a **byte offset** and gets only what was appended since, so the usual
  tick reads nothing at all; `read_since()` reports `reset` when that cursor is no
  good (midnight rotation, truncation, or a burst big enough that honouring it would
  mean an unbounded read) and the client replaces instead of appending.
- **Three scroll rules, and all three were asked for explicitly.** Follow the newest
  entry; if the reader has scrolled up, keep streaming but **don't** yank them down;
  when they scroll back to the end, re-sync — which falls out of re-checking
  "am I at the bottom" on every tick rather than tracking a mode. **`.log-view` sets
  `overflow-anchor: none` on purpose**: trimming entries off the top makes browsers
  that implement scroll anchoring adjust `scrollTop` themselves, and Safari (which
  doesn't implement it) would not — so the compensation is done in JS for everyone,
  and leaving anchoring on applied it twice and slid the text down by the height of
  whatever was trimmed. Found in a browser; no unit test would have seen it.
- **The page needs JavaScript only for the live tail.** The filters are a plain `GET`
  form with an Apply button rather than a `<select onchange>`, matching the
  dependency-free admin-side convention, and the Live switch is hidden unless JS
  reveals it — a switch that does nothing is worse than no switch. `.log-panel` opts out of `.form-panel`'s 560px cap on purpose: that cap
  is right for a form and wrong for a log, where every extra pixel is one less
  wrapped traceback line. The log box scrolls inside itself (`overflow: auto` +
  `white-space: pre-wrap`) so a long line never pushes the page sideways — verified
  across 320–1440px with the responsive audit described under *Admin panel
  navigation*.
