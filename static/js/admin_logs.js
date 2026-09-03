// The log view: opens at the newest entry, and keeps itself up to date.
//
// Two behaviours, and the second one exists because a log page you have to refresh
// is only half a log page.
//
// 1. Scroll to the end on load. Entries are rendered oldest-first - that is the
//    order a log reads in, and a traceback has to be read downwards - but the reason
//    you opened the page is almost always at the bottom.
// 2. Poll for what has been appended since, and add it. The server is handed a byte
//    offset and answers with an HTML fragment of whatever came after it, so the usual
//    poll transfers nothing; see admin_logs_tail() for why this polls rather than
//    holding a streaming connection open.
(function () {
  var view = document.querySelector('[data-log-view]');
  if (!view) return;

  var STICK_SLACK_PX = 40;   // "close enough to the bottom to count as following"
  var POLL_MS = 5000;
  var toggle = document.querySelector('[data-log-live]');
  var liveNote = document.querySelector('.log-live-note');
  var counter = document.querySelector('[data-log-count]');
  var timer = null;
  var busy = false;

  function atBottom() {
    return view.scrollHeight - view.scrollTop - view.clientHeight <= STICK_SLACK_PX;
  }

  function toBottom() {
    view.scrollTop = view.scrollHeight;
  }

  toBottom();

  // The controls only work with JavaScript, so they are revealed by it.
  var wrapper = toggle && toggle.closest('.log-live');
  if (wrapper) wrapper.hidden = false;
  if (liveNote) liveNote.hidden = false;

  function trim() {
    // Left running for hours, an append-only view would grow without bound. The
    // page's own size setting is the cap, applied from the top - the newest entries
    // are the ones worth keeping.
    var limit = parseInt(view.getAttribute('data-log-limit'), 10) || 200;
    var entries = view.querySelectorAll('.log-entry');
    for (var i = 0; i < entries.length - limit; i++) entries[i].remove();
  }

  function poll() {
    if (busy || document.hidden) return;   // a hidden tab has nobody reading it
    busy = true;
    var url = view.getAttribute('data-log-url') +
      '?since=' + encodeURIComponent(view.getAttribute('data-log-offset')) +
      '&limit=' + encodeURIComponent(view.getAttribute('data-log-limit')) +
      '&level=' + encodeURIComponent(view.getAttribute('data-log-level'));

    fetch(url, {credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}})
      .then(function (resp) { return resp.ok ? resp.text() : null; })
      .then(function (html) {
        if (html === null) return stop();
        var holder = document.createElement('div');
        holder.innerHTML = html;
        var fragment = holder.querySelector('[data-log-fragment]');
        // No fragment means this wasn't the endpoint answering - an expired session
        // redirected to the login page, most likely. Polling on would be pointless.
        if (!fragment) return stop();

        var wasFollowing = atBottom();
        view.setAttribute('data-log-offset', fragment.getAttribute('data-log-offset'));
        if (fragment.getAttribute('data-log-reset') === '1') {
          view.innerHTML = fragment.innerHTML;
        } else {
          while (fragment.firstChild) view.appendChild(fragment.firstChild);
        }
        // Trimming removes entries from the *top*, which shifts everything below it
        // up by their height - so someone reading further up would find the text
        // sliding out from under them every time a new line arrived. Measuring the
        // height that went and compensating keeps the same content under their eyes.
        var heightBefore = view.scrollHeight;
        trim();
        var removed = heightBefore - view.scrollHeight;
        if (!wasFollowing && removed > 0) {
          view.scrollTop = Math.max(0, view.scrollTop - removed);
        }
        if (counter) counter.textContent = view.querySelectorAll('.log-entry').length;
        // Only follow if they were already at the bottom. Yanking the view down while
        // someone is reading something further up is worse than not updating at all.
        if (wasFollowing) toBottom();
      })
      .catch(function () { /* a blip; the next tick tries again */ })
      .then(function () { busy = false; });
  }

  function start() {
    if (timer) return;
    timer = setInterval(poll, POLL_MS);
  }

  function stop() {
    clearInterval(timer);
    timer = null;
  }

  if (toggle) {
    toggle.addEventListener('change', function () {
      if (toggle.checked) { poll(); start(); } else { stop(); }
    });
  }
  // Catch up immediately on becoming visible again rather than waiting out a tick.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!toggle || toggle.checked)) poll();
  });
  if (!toggle || toggle.checked) start();
})();
