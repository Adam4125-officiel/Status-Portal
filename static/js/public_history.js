(function () {
  // "Load more" buttons for incidents and maintenance history - fetches the next
  // page as a server-rendered HTML fragment (this app has no client-side
  // templating anywhere else, so this matches that convention rather than
  // introducing a JSON+template path just for this) and inserts it right before
  // the button itself. An empty response means there's nothing more to load, so
  // the button removes itself instead of staying around to produce empty clicks.
  var PAGE_SIZE = 10;

  function wire(button) {
    button.addEventListener('click', function () {
      var offset = parseInt(button.getAttribute('data-offset'), 10) || 0;
      button.disabled = true;
      fetch(button.getAttribute('data-url') + '?offset=' + offset)
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!html.trim()) {
            button.remove();
            return;
          }
          button.insertAdjacentHTML('beforebegin', html);
          // Re-scanning the whole page is cheap here (at most a few dozen
          // timestamps) and avoids fragile DOM traversal to scope it to just
          // what was inserted.
          if (window.applyLocalTimes) window.applyLocalTimes(document);
          button.setAttribute('data-offset', offset + PAGE_SIZE);
          button.textContent = 'Load more';
          button.disabled = false;
        })
        .catch(function () {
          button.disabled = false;
        });
    });
  }

  document.querySelectorAll('.load-more-btn').forEach(wire);
})();
