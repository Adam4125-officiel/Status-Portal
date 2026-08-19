(function () {
  // Client half of admin_system_clear_browser_cache() - see that route's docstring
  // for why this exists alongside the Clear-Site-Data header rather than instead of
  // it (the header is ignored on plain HTTP in Chrome/Edge, which is how this portal
  // is very often served).
  var panel = document.getElementById('clear-browser-cache');
  if (!panel) return;

  var statusEl = document.getElementById('clear-browser-cache-status');
  var assets = [];
  try {
    assets = JSON.parse(panel.getAttribute('data-assets') || '[]');
  } catch (e) {
    assets = [];
  }
  var theme = panel.getAttribute('data-theme') || '';
  var doneUrl = panel.getAttribute('data-done-url') || '/admin/system';

  function say(text) {
    if (statusEl) statusEl.textContent = text;
  }

  // Cache Storage (what a service worker / PWA install would hold). Nothing in this
  // app registers one today, but a browser that picked one up from an earlier
  // version - or from anything else ever served on this origin and port, which on a
  // home server is a very real possibility - would otherwise keep serving from it.
  function clearCacheStorage() {
    if (!window.caches || !caches.keys) return Promise.resolve();
    return caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) { return caches.delete(k); }));
    }).catch(function () {});
  }

  function unregisterServiceWorkers() {
    if (!navigator.serviceWorker || !navigator.serviceWorker.getRegistrations) {
      return Promise.resolve();
    }
    return navigator.serviceWorker.getRegistrations().then(function (regs) {
      return Promise.all(regs.map(function (r) { return r.unregister(); }));
    }).catch(function () {});
  }

  function clearStorage() {
    try { sessionStorage.clear(); } catch (e) {}
    try {
      localStorage.clear();
      // The one thing deliberately put back: the dark/light choice is a preference
      // the admin set on purpose, not stale data, and losing it on a cache clear
      // would read as a bug. Restored after the wipe rather than skipped by it, so
      // the Clear-Site-Data header (which runs before any of this) can't keep it.
      if (theme) localStorage.setItem('portal-theme', theme);
    } catch (e) {}
  }

  // The part that actually replaces what the browser has stored: cache:'reload'
  // forces a network fetch that bypasses the HTTP cache *and* writes the fresh
  // response back into it, so the next ordinary request gets the new file.
  function refetchAssets() {
    if (!window.fetch || !assets.length) return Promise.resolve();
    return Promise.all(assets.map(function (url) {
      return fetch(url, { cache: 'reload', credentials: 'same-origin' }).catch(function () {});
    }));
  }

  say('Clearing stored data…');
  clearCacheStorage()
    .then(unregisterServiceWorkers)
    .then(function () {
      clearStorage();
      say('Re-downloading ' + assets.length + ' file(s)…');
      return refetchAssets();
    })
    .then(function () {
      say('Done — returning to the System page.');
      // replace() rather than assign(): this page is a one-shot action, and leaving
      // it in history means the Back button re-runs the whole thing.
      window.location.replace(doneUrl);
    })
    .catch(function () {
      say('Finished with errors — see the browser console. Returning anyway.');
      window.location.replace(doneUrl);
    });
})();
