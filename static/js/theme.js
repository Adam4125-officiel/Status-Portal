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

  var current = localStorage.getItem('portal-theme') || 'dark';
  apply(current);

  btn.addEventListener('click', function () {
    current = current === 'light' ? 'dark' : 'light';
    localStorage.setItem('portal-theme', current);
    apply(current);
  });
})();
