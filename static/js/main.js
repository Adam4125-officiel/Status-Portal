(function () {
  var REFRESH_SECONDS = window.PORTAL_REFRESH_SECONDS || 60;
  var remaining = REFRESH_SECONDS;
  var el = document.getElementById('refresh-countdown');

  // A reload throws away whatever the person is in the middle of: a half-typed 2FA code,
  // and the confirmation panel it was typed into (it is shown by a script, so a fresh page
  // starts with it closed). So the page holds off while it is being used:
  //  - any element marked data-holds-refresh is on screen (the VM / host / restart panels);
  //  - a text field has the focus.
  // The countdown restarts from the full interval while held, so closing the panel does not
  // reload the page at once either.
  var TEXT_LESS_INPUTS = /^(button|submit|reset|checkbox|radio|file|image|range|color)$/i;

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

  setInterval(function () {
    if (document.hidden) return; // don't bother refreshing while the tab isn't visible
    if (inUse()) {
      remaining = REFRESH_SECONDS;
      if (el) el.textContent = 'paused';
      return;
    }
    remaining -= 1;
    if (remaining <= 0) {
      window.location.reload();
      return;
    }
    if (el) el.textContent = remaining + 's';
  }, 1000);
})();
