(function () {
  var REFRESH_SECONDS = 60;
  var remaining = REFRESH_SECONDS;
  var el = document.getElementById('refresh-countdown');

  setInterval(function () {
    if (document.hidden) return; // don't bother refreshing while the tab isn't visible
    remaining -= 1;
    if (remaining <= 0) {
      window.location.reload();
      return;
    }
    if (el) el.textContent = remaining + 's';
  }, 1000);
})();
