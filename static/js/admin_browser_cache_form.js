(function () {
  // Carries the current dark/light choice across the clear, so the wipe on the next
  // page can put it back. Read here rather than server-side because the theme lives
  // only in the browser's localStorage - the server has never known about it.
  var field = document.getElementById('clear-browser-cache-theme');
  if (!field) return;
  try {
    field.value = localStorage.getItem('portal-theme') || '';
  } catch (e) {
    field.value = '';
  }
})();
