(function () {
  // Server-rendered timestamps are UTC (the server has no idea what timezone a
  // visitor is in) - each one keeps its raw ISO string in data-utc as a fallback
  // (and for no-JS clients), and this swaps the displayed text to the browser's
  // own local time/timezone on load.
  var FULL_FORMAT = {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  };
  // Compact form for tight spots (e.g. a service card's "upd. HH:MM") where a full
  // date + timezone name would be more clutter than the space is worth - just the
  // local clock time, still genuinely local rather than UTC.
  var SHORT_FORMAT = { hour: '2-digit', minute: '2-digit' };
  // Date-first form, for things that happen on a *day* rather than at a moment - a
  // release calendar being the case this exists for. Using SHORT_FORMAT there rendered
  // "03:00" with no date at all, which is the least useful possible answer to "when is
  // this coming out".
  var DATE_FORMAT = { year: 'numeric', month: 'short', day: 'numeric' };

  function convert(root, selector, format) {
    root.querySelectorAll(selector).forEach(function (el) {
      var parsed = new Date(el.getAttribute('data-utc'));
      if (isNaN(parsed.getTime())) return; // leave the UTC fallback text as-is
      el.textContent = parsed.toLocaleString(undefined, format);
    });
  }

  // Exposed so content inserted after load (e.g. "load more" fragments in
  // public_history.js) can be converted too, without re-running this whole file -
  // scoped to whatever root element is passed in, defaulting to the full page for
  // this initial on-load pass.
  window.applyLocalTimes = function (root) {
    root = root || document;
    convert(root, '.local-time[data-utc]', FULL_FORMAT);
    convert(root, '.local-time-short[data-utc]', SHORT_FORMAT);
    convert(root, '.local-date[data-utc]', DATE_FORMAT);
  };

  window.applyLocalTimes(document);
})();
