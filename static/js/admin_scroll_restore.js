// Keeps your place on the page when a form submits.
//
// Every admin form is a POST that redirects back to the same page (or a GET form
// that reloads it with new query params), and a browser starts a fresh page at the
// top. So saving one field at the bottom of Settings, or pressing Apply on the log
// filters, threw you back to the top of the page every single time and you had to
// scroll down again to carry on. That is the bug this fixes; it is not specific to
// any one page, which is why it is loaded for the whole admin panel.
//
// Deliberately keyed on the path, and deliberately short-lived: a restore only
// happens on the same page you submitted from, and only if the load follows within
// a few seconds. Navigating away and coming back later starts at the top, the way a
// fresh visit should.
(function () {
  var KEY = 'admin-scroll-restore';
  var MAX_AGE_MS = 10000;

  function remember() {
    try {
      sessionStorage.setItem(KEY, JSON.stringify({
        path: location.pathname,
        y: window.scrollY || document.documentElement.scrollTop || 0,
        at: Date.now()
      }));
    } catch (e) {
      // Private mode, or storage disabled. Losing the scroll position is a small
      // annoyance; throwing here would break the submit itself, which is not.
    }
  }

  // The submit event bubbles, so one listener covers every form on the page -
  // including any added later. Capture phase so a handler that stops propagation
  // (or removes the form) can't cost us the position.
  document.addEventListener('submit', remember, true);

  function restore() {
    var raw;
    try {
      raw = sessionStorage.getItem(KEY);
      sessionStorage.removeItem(KEY);  // one-shot: a later plain visit starts at the top
    } catch (e) {
      return;
    }
    if (!raw) return;
    var saved;
    try {
      saved = JSON.parse(raw);
    } catch (e) {
      return;
    }
    if (!saved || saved.path !== location.pathname) return;
    if (Date.now() - saved.at > MAX_AGE_MS) return;
    if (!saved.y) return;
    // The browser restores its own scroll on a back/forward navigation; overriding
    // that would fight the user rather than help them.
    if (window.performance && performance.getEntriesByType) {
      var nav = performance.getEntriesByType('navigation')[0];
      if (nav && nav.type === 'back_forward') return;
    }
    window.scrollTo(0, saved.y);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restore);
  } else {
    restore();
  }
})();
