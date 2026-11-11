// Keeps a public page current without reloading it.
//
// Every refresh interval the page's live region (#live-region) is re-fetched from the
// same address with ?fragment=1 and its contents swapped in place, so the visitor keeps
// their scroll position and whatever "Load more" had already unfolded. The server
// answers with an ETag and a 304 when nothing changed; the browser replays a 304 as a
// plain 200 with the same ETag header, and an unchanged tag means the swap is skipped.
// (The admin resources page still uses main.js, which reloads.)
//
//  - 0 seconds means the page opted out (forms, search results, error pages): nothing runs.
//  - Nothing is fetched while the tab is hidden; coming back to it refreshes at once if
//    the data is older than the interval.
//  - While the page is in use the poll waits: an element marked data-holds-refresh is on
//    screen (the confirmation panels), or a text field has focus (a half-typed search).
(function () {
  var REFRESH_SECONDS = window.PORTAL_REFRESH_SECONDS;
  if (!REFRESH_SECONDS) return;
  var region = document.getElementById('live-region');
  if (!region) return;
  var statusEl = document.getElementById('refresh-status');
  var url = region.getAttribute('data-fragment-url');

  var TEXT_LESS_INPUTS = /^(button|submit|reset|checkbox|radio|file|image|range|color)$/i;
  var lastOkAt = Date.now();
  var lastEtag = null;
  var lastFailed = false;
  var inFlight = false;

  function inUse() {
    var held = document.querySelectorAll('[data-holds-refresh]');
    for (var i = 0; i < held.length; i++) {
      if (held[i].getClientRects().length > 0) return true;
    }
    var active = document.activeElement;
    if (!active) return false;
    if (active.tagName === 'TEXTAREA' || active.tagName === 'SELECT') return true;
    return active.tagName === 'INPUT' && !TEXT_LESS_INPUTS.test(active.type || '');
  }

  function ago(seconds) {
    if (seconds < 5) return 'just now';
    if (seconds < 60) return seconds + ' s ago';
    return Math.floor(seconds / 60) + ' min ago';
  }

  function renderStatus() {
    if (!statusEl) return;
    var text = 'Updated ' + ago(Math.round((Date.now() - lastOkAt) / 1000));
    statusEl.textContent = lastFailed ? text + ' (retrying)' : text;
  }

  // What "Load more" added lives in .load-more-batch wrappers (public_history.js). The
  // fresh fragment has only the first page, so the batches are put back in front of their
  // buttons - minus anything the first page now shows itself - and the buttons are
  // brought back to where they were (next offset, or gone when nothing was left).
  function collectBatches() {
    var batches = region.querySelectorAll('.load-more-batch');
    var saved = [];
    for (var i = 0; i < batches.length; i++) {
      var key = batches[i].getAttribute('data-for');
      var button = document.getElementById(key);
      saved.push({
        key: key,
        node: batches[i],
        offset: button ? button.getAttribute('data-offset') : null,
        exhausted: !button,
      });
    }
    return saved;
  }

  function restoreBatches(saved) {
    saved.forEach(function (batch) {
      var button = document.getElementById(batch.key);
      if (!button) return;
      var shown = {};
      region.querySelectorAll('[data-id]').forEach(function (el) {
        shown[el.getAttribute('data-id')] = true;
      });
      batch.node.querySelectorAll('[data-id]').forEach(function (el) {
        if (shown[el.getAttribute('data-id')]) el.remove();
      });
      button.parentNode.insertBefore(batch.node, button);
      if (batch.exhausted) {
        button.remove();
      } else {
        if (batch.offset !== null) button.setAttribute('data-offset', batch.offset);
        button.textContent = 'Load more';
      }
    });
  }

  function swap(html) {
    var saved = collectBatches();
    region.innerHTML = html;
    restoreBatches(saved);
    if (window.applyLocalTimes) window.applyLocalTimes(region);
    if (window.wireLoadMore) window.wireLoadMore(region);
  }

  function poll() {
    if (inFlight) return;
    inFlight = true;
    fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        var etag = response.headers.get('ETag');
        if (etag && etag === lastEtag) return null;
        lastEtag = etag;
        return response.text();
      })
      .then(function (html) {
        if (html !== null) swap(html);
        lastOkAt = Date.now();
        lastFailed = false;
      })
      .catch(function () { lastFailed = true; })
      .then(function () {
        inFlight = false;
        renderStatus();
      });
  }

  function due() {
    return !document.hidden && Date.now() - lastOkAt >= REFRESH_SECONDS * 1000;
  }

  setInterval(function () {
    renderStatus();
    // Counted from the last success, so a held poll (typing) goes through as soon as the
    // person stops, and a failed one is retried on the next tick rather than a minute on.
    if (due() && !inUse()) poll();
  }, 1000);

  document.addEventListener('visibilitychange', function () {
    if (due() && !inUse()) poll();
  });
})();
