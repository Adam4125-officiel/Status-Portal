(function () {
  // Server-rendered timestamps are UTC (the server has no idea what timezone a
  // visitor is in) - each one keeps its raw ISO string in data-utc as a fallback
  // (and for no-JS clients), and this swaps the displayed text to the browser's
  // own local time/timezone on load.
  var FORMAT = {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  };
  document.querySelectorAll('.local-time[data-utc]').forEach(function (el) {
    var parsed = new Date(el.getAttribute('data-utc'));
    if (isNaN(parsed.getTime())) return; // leave the "... UTC" fallback text as-is
    el.textContent = parsed.toLocaleString(undefined, FORMAT);
  });
})();
