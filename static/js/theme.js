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

  // No saved choice yet -> follow the OS/browser preference; an explicit toggle
  // click (below) always overrides it from then on via localStorage.
  var saved = localStorage.getItem('portal-theme');
  var osPrefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  var current = saved || (osPrefersLight ? 'light' : 'dark');
  apply(current);

  btn.addEventListener('click', function () {
    current = current === 'light' ? 'dark' : 'light';
    localStorage.setItem('portal-theme', current);
    apply(current);
  });
})();
