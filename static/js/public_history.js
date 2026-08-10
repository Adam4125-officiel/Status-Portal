(function () {
  // "Load more" buttons for incidents and maintenance history - fetches the next
  // page as a server-rendered HTML fragment (this app has no client-side
  // templating anywhere else, so this matches that convention rather than
  // introducing a JSON+template path just for this) and inserts it right before
  // the button itself. An empty response means there's nothing more to load, so
  // the button removes itself instead of staying around to produce empty clicks.
  //
  // Two pagination styles, picked per-button by which data attribute is present:
  // - data-seen-selector (incidents): sends the ids already rendered on the page
  //   (?seen=5,4,3) and gets back whatever is left. NOT an offset and NOT an id
  //   cursor - the initial incident list is age-filtered, and neither of those
  //   can express "everything I'm not already showing" against a filtered view
  //   without skipping hidden items or re-appending visible ones (both shipped
  //   and both were real bugs; see db.list_incidents()).
  // - data-offset (maintenance history): a plain numeric offset, safe here
  //   because every call into that endpoint uses the same unfiltered query, so
  //   there's no filtered/unfiltered mismatch for an offset to drift against.
  var PAGE_SIZE = 10;

  function seenIds(selector) {
    return Array.prototype.map.call(
      document.querySelectorAll(selector),
      function (el) { return el.getAttribute('data-id'); }
    ).filter(Boolean);
  }

  function wire(button) {
    button.addEventListener('click', function () {
      var seenSelector = button.getAttribute('data-seen-selector');
      var url = button.getAttribute('data-url');
      var seenBefore = 0;
      if (seenSelector) {
        var ids = seenIds(seenSelector);
        seenBefore = ids.length;
        url += '?seen=' + ids.join(',');
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
          if (seenSelector) {
            // If the page didn't actually gain anything, stop rather than let
            // further clicks re-append the same batch forever - the failure mode
            // a stale cached copy of this file caused against a newer server.
            if (seenIds(seenSelector).length <= seenBefore) {
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
