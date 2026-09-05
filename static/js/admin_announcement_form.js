(function () {
  // Deliberately no pre-filling, unlike admin_maintenance_form.js. A maintenance
  // window must have both ends, so filling them in is a convenience; an announcement's
  // window is optional, and blank means "show it from now until I delete it" - the
  // behaviour every announcement had before the window existed. Pre-filling would
  // silently opt every new announcement into an expiry nobody asked for.
  var starts = document.getElementById('starts_at');
  var ends = document.getElementById('ends_at');
  var clear = document.getElementById('window_clear');
  if (!starts || !ends || !clear) return;

  clear.addEventListener('click', function (e) {
    e.preventDefault();
    starts.value = '';
    ends.value = '';
  });
})();
