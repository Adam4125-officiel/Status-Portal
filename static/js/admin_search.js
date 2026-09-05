(function () {
  // The floating admin search. Two halves that share this file because they are two
  // ends of one journey: finding a control anywhere in the panel, and then actually
  // arriving at it on the page it lives on.
  //
  // Results come from the server as a rendered HTML fragment (/admin/search), matching
  // this app's convention everywhere else - /api/incidents/more, /admin/logs/tail,
  // /search/live. There is no client-side templating anywhere in this project and a
  // search box is not the place to start.
  var DEBOUNCE_MS = 130;

  // -------------------------------------------------------------------------
  // Arriving at a control: ?jump=<input name>
  // -------------------------------------------------------------------------
  // Keyed on the input's `name` rather than an id, so a result can link to any control
  // without several hundred fields across twenty templates each needing an id added
  // just to be linkable. Runs on every admin page, independently of the search UI.
  (function jumpToRequestedField() {
    var name = new URLSearchParams(window.location.search).get('jump');
    if (!name) return;
    // By name first, then by id: most controls are keyed on their `name`, but a few
    // (the step-up 2FA prompts) only carry an id, which is what the index picked up
    // from the label's `for=`.
    var safe = (window.CSS && CSS.escape) ? CSS.escape(name) : name;
    var input = document.querySelector('[name="' + safe + '"]') || document.getElementById(name);
    if (!input) return;
    var field = input.closest('.field') || input;
    // Inside a collapsed <details> (the wizard's advanced block, a task's schedule
    // form), open it first - otherwise the page scrolls to something invisible and
    // looks broken.
    var parent = field.parentElement;
    while (parent) {
      if (parent.tagName === 'DETAILS') parent.open = true;
      parent = parent.parentElement;
    }
    field.scrollIntoView({ block: 'center', behavior: 'smooth' });
    field.classList.add('admin-jump-target');
    // Removed after the animation so a later re-render or a saved form doesn't leave a
    // permanent highlight on a field nobody is looking for any more.
    setTimeout(function () { field.classList.remove('admin-jump-target'); }, 2600);
    if (input.focus) {
      try { input.focus({ preventScroll: true }); } catch (e) { /* older browsers */ }
    }
  })();

  // -------------------------------------------------------------------------
  // The search UI
  // -------------------------------------------------------------------------
  var wrap = document.getElementById('admin_search');
  var openBtn = document.getElementById('admin_search_open');
  var panel = document.getElementById('admin_search_panel');
  var input = document.getElementById('admin_search_input');
  var output = document.getElementById('admin_search_output');
  var script = document.querySelector('script[data-search-url]');
  if (!wrap || !openBtn || !panel || !input || !output || !script) return;
  var searchUrl = script.getAttribute('data-search-url');

  var timer = null;
  var controller = null;

  function results() {
    return Array.prototype.slice.call(output.querySelectorAll('.admin-search__result'));
  }

  function highlight(index) {
    var items = results();
    items.forEach(function (el, i) {
      el.classList.toggle('is-active', i === index);
      if (i === index) el.scrollIntoView({ block: 'nearest' });
    });
  }

  function activeIndex() {
    return results().findIndex(function (el) { return el.classList.contains('is-active'); });
  }

  function fetchResults(query) {
    // Only the newest question matters, so an in-flight request for a stale one is
    // abandoned rather than raced - otherwise a slow response can overwrite a newer
    // one and show results for something you already finished typing.
    if (controller) controller.abort();
    controller = new AbortController();
    fetch(searchUrl + '?q=' + encodeURIComponent(query), {
      signal: controller.signal,
      headers: { 'X-Requested-With': 'fetch' },
    })
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (htmlText) {
        if (!htmlText) return;
        output.innerHTML = htmlText;
        highlight(0);
      })
      .catch(function (e) {
        if (e.name !== 'AbortError') {
          output.innerHTML = '<p class="admin-search__empty">Search is unavailable right now.</p>';
        }
      });
  }

  function open() {
    if (!panel.hidden) return;
    panel.hidden = false;
    openBtn.setAttribute('aria-expanded', 'true');
    wrap.classList.add('is-open');
    input.focus();
    input.select();
    if (!output.innerHTML.trim()) fetchResults('');
  }

  function close() {
    panel.hidden = true;
    openBtn.setAttribute('aria-expanded', 'false');
    wrap.classList.remove('is-open');
  }

  openBtn.addEventListener('click', function () {
    if (panel.hidden) { open(); } else { close(); }
  });

  input.addEventListener('input', function () {
    clearTimeout(timer);
    var query = input.value;
    timer = setTimeout(function () { fetchResults(query); }, DEBOUNCE_MS);
  });

  input.addEventListener('keydown', function (e) {
    var items = results();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!items.length) return;
      e.preventDefault();
      var next = activeIndex() + (e.key === 'ArrowDown' ? 1 : -1);
      if (next < 0) next = items.length - 1;
      if (next >= items.length) next = 0;
      highlight(next);
    } else if (e.key === 'Enter') {
      var current = items[activeIndex()];
      if (current) {
        e.preventDefault();
        window.location.href = current.getAttribute('href');
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
      openBtn.focus();
    }
  });

  // Clicking away closes it. Kept on the document rather than a backdrop element so the
  // rest of the page stays usable while the panel is open.
  document.addEventListener('click', function (e) {
    if (!panel.hidden && !wrap.contains(e.target)) close();
  });

  document.addEventListener('keydown', function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)
      || document.activeElement.isContentEditable;
    // Ctrl/Cmd+K works anywhere; bare "/" only when not already typing, so it can still
    // be typed into a URL field or a message box.
    if ((e.key === 'k' || e.key === 'K') && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      open();
    } else if (e.key === '/' && !typing) {
      e.preventDefault();
      open();
    }
  });

  wrap.hidden = false;
})();
