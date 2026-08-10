(function () {
  // "Load more" buttons for incidents and maintenance history - fetches the next
  // page as a server-rendered HTML fragment (this app has no client-side
  // templating anywhere else, so this matches that convention rather than
  // introducing a JSON+template path just for this) and inserts it right before
  // the button itself. An empty response means there's nothing more to load, so
  // the button removes itself instead of staying around to produce empty clicks.
  //
  // Two pagination styles, picked per-button by which data attribute is present:
  // - data-before-id (incidents): an id cursor - "give me the next page after
  //   this id" - server-side never re-applies the initial view's age filter, so
  //   older/hidden incidents stay reachable. Advanced after each fetch to the
  //   last inserted item's own data-id.
  // - data-offset (maintenance history): a plain numeric offset - safe here
  //   because every call into that endpoint uses the same unfiltered query, so
  //   there's no filtered/unfiltered mismatch for an offset to drift against.
  var PAGE_SIZE = 10;

  function wire(button) {
    button.addEventListener('click', function () {
      var cursorMode = button.hasAttribute('data-before-id');
      var url = button.getAttribute('data-url');
      if (cursorMode) {
        var beforeId = button.getAttribute('data-before-id');
        url += '?before_id=' + beforeId;
      } else {
        var offset = parseInt(button.getAttribute('data-offset'), 10) || 0;
        url += '?offset=' + offset;
      }
      button.disabled = true;
      fetch(url)
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
          if (cursorMode) {
            // The last item inserted is the one immediately before the button
            // now (insertAdjacentHTML keeps fragment order) - it carries the
            // smallest id of this batch, exactly the right cursor to continue
            // further back in time on the next click.
            var lastItem = button.previousElementSibling;
            var lastId = lastItem ? lastItem.getAttribute('data-id') : null;
            if (lastId) {
              button.setAttribute('data-before-id', lastId);
            } else {
              button.remove();
              return;
            }
          } else {
            button.setAttribute('data-offset', offset + PAGE_SIZE);
          }
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
