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

  function convert(selector, format) {
    document.querySelectorAll(selector).forEach(function (el) {
      var parsed = new Date(el.getAttribute('data-utc'));
      if (isNaN(parsed.getTime())) return; // leave the UTC fallback text as-is
      el.textContent = parsed.toLocaleString(undefined, format);
    });
  }

  convert('.local-time[data-utc]', FULL_FORMAT);
  convert('.local-time-short[data-utc]', SHORT_FORMAT);
})();
