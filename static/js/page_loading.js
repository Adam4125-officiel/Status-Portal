// Shows a loading bar the moment a public-page link is clicked.
//
// These pages are server-rendered, so the browser shows the *old* page until the new
// one arrives - and with nothing moving, a slow response reads as "stuck". This makes
// the wait visible and intentional. It deliberately doesn't try to be client-side
// navigation: the pages are fast, the HTML is complete on arrival, and swapping in
// fragments would mean breaking the no-JS path for a spinner.
(function () {
  var bar = document.createElement('div');
  bar.className = 'page-loading';
  bar.setAttribute('role', 'status');
  bar.setAttribute('aria-label', 'Loading');
  var shown = false;

  function show() {
    if (shown) return;
    shown = true;
    document.body.appendChild(bar);
    // Next frame, so the transition actually animates from 0.
    requestAnimationFrame(function () { bar.classList.add('page-loading--on'); });
  }

  function isPlainLeftClick(e) {
    // A middle click, or ctrl/cmd-click, opens a new tab - this page isn't going
    // anywhere, so showing a loading bar on it would be a lie.
    return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
  }

  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('.page-nav a, .summary-card');
    if (!link || !isPlainLeftClick(e) || e.defaultPrevented) return;
    if (link.target === '_blank' || link.hasAttribute('download')) return;
    var href = link.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#') return;
    show();
  });

  // Back/forward restores from the cache with the bar still painted on it, which would
  // leave a permanent stripe across the top of the page.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted && bar.parentNode) {
      bar.parentNode.removeChild(bar);
      bar.classList.remove('page-loading--on');
      shown = false;
    }
  });
})();
