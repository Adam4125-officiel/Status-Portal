// Incremental search: results appear a moment after you stop typing, without pressing
// anything.
//
// The form still works exactly as before with JavaScript off - this only adds to it.
// The fragment fetched here is rendered from the same template as the submitted
// results, so what you see while typing and what you see after pressing Search cannot
// drift apart.
(function () {
  var box = document.getElementById('search-results');
  var input = document.querySelector('.search-form input[type="search"]');
  if (!box || !input || !window.fetch) return;

  var url = box.getAttribute('data-live-url');
  var minLength = parseInt(box.getAttribute('data-min-length'), 10) || 3;
  var DEBOUNCE_MS = 350;
  var timer = null;
  var inFlight = null;
  var lastQuery = input.value.trim();

  function run(query) {
    // Only the newest keystroke matters; anything still in flight is answering a
    // question that's already been replaced, and every request costs two API calls.
    if (inFlight) inFlight.abort();
    var controller = new AbortController();
    inFlight = controller;
    box.classList.add('search-results--loading');

    fetch(url + '?q=' + encodeURIComponent(query), {
      signal: controller.signal,
      headers: { 'X-Requested-With': 'fetch' },
      credentials: 'same-origin'
    }).then(function (r) {
      return r.ok ? r.text() : null;
    }).then(function (html) {
      if (html === null) return;
      box.innerHTML = html;
      // Newly inserted timestamps arrive after the page's load event fired.
      if (window.applyLocalTimes) window.applyLocalTimes(box);
      // Keep the address bar in step, so a reload or a shared link shows the same
      // thing - without adding an entry to history for every keystroke.
      try {
        history.replaceState(null, '', query ? '?q=' + encodeURIComponent(query) : location.pathname);
      } catch (e) { /* a browser that refuses this simply keeps the old URL */ }
    }).catch(function (e) {
      if (e.name !== 'AbortError') box.classList.remove('search-results--loading');
    }).then(function () {
      if (inFlight === controller) {
        inFlight = null;
        box.classList.remove('search-results--loading');
      }
    });
  }

  input.addEventListener('input', function () {
    var query = input.value.trim();
    if (query === lastQuery) return;
    lastQuery = query;
    clearTimeout(timer);
    if (query.length < minLength) return;
    timer = setTimeout(function () { run(query); }, DEBOUNCE_MS);
  });

  // Submitting mid-debounce should search immediately rather than wait out the pause.
  var form = input.closest('form');
  if (form) {
    form.addEventListener('submit', function () { clearTimeout(timer); });
  }
})();
