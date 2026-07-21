(function () {
  var REFRESH_SECONDS = 60;
  var remaining = REFRESH_SECONDS;
  var el = document.getElementById('refresh-countdown');

  setInterval(function () {
    if (document.hidden) return; // ne dérange pas si l'onglet n'est pas visible
    remaining -= 1;
    if (remaining <= 0) {
      window.location.reload();
      return;
    }
    if (el) el.textContent = remaining + 's';
  }, 1000);
})();
