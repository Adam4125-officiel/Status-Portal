(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;

  function apply(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    btn.textContent = theme === 'light' ? '🌙' : '☀️';
  }

  // Precedence, matching the inline FOUC script in base.html exactly - if these two
  // ever disagree, the page visibly changes colour a moment after it loads:
  //   1. this browser's own saved choice (the most recent deliberate action here),
  //   2. the signed-in user's account-level preference (data-server-theme),
  //   3. the OS/browser preference.
  var saved = localStorage.getItem('portal-theme');
  var server = document.documentElement.getAttribute('data-server-theme');
  var osPrefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  var current = saved || server || (osPrefersLight ? 'light' : 'dark');
  apply(current);

  // Signed in, a toggle here also updates the account preference, so the choice
  // follows the user to their other devices instead of living only in this browser.
  // Fire-and-forget: the page has already applied the change, and a failed sync just
  // means this device disagrees with the others until the next toggle - never a
  // visible error, and never a reason to block the click.
  function remember(theme) {
    if (!window.PORTAL_SIGNED_IN) return;
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta || !meta.content) return;
    try {
      fetch('/account/theme', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'theme=' + encodeURIComponent(theme) + '&csrf_token=' + encodeURIComponent(meta.content),
        credentials: 'same-origin'
      }).catch(function () {});
    } catch (e) { /* no fetch, or blocked - the local choice still applies */ }
  }

  btn.addEventListener('click', function () {
    current = current === 'light' ? 'dark' : 'light';
    localStorage.setItem('portal-theme', current);
    apply(current);
    remember(current);
  });
})();
