// Stops the request configuration form being submitted twice.
//
// This is the first of three layers against a duplicate request, and the only one that
// can stop the second POST ever being sent: a double-click on "Request" fires two
// submissions within milliseconds of each other, so both are already in flight before
// the server has written anything either of them could be checked against. The other
// two catch what this can't - app.py's per-session guard covers a refresh or a
// back-and-resubmit minutes later, and Seerr's own duplicate check (which only works
// because request_via_seerr() now sends is4k - see its docstring) covers another
// device entirely.
//
// No-JS still works: without this the form submits normally, and the two server-side
// layers are what actually protect Seerr. This only removes the easiest way to trip it.
(function () {
  var form = document.querySelector('form[data-request-form]');
  if (!form) return;
  var button = form.querySelector('button[type="submit"]');
  var submitting = false;

  form.addEventListener('submit', function (e) {
    if (submitting) {
      e.preventDefault();
      return;
    }
    submitting = true;
    if (!button) return;
    // Deferred by a tick rather than done inline: a disabled control is not submitted,
    // and disabling it during the submit event can drop it from the payload in some
    // browsers. The button carries no name/value here, but relying on that would make
    // this quietly break if one were ever added.
    setTimeout(function () {
      button.disabled = true;
      button.textContent = 'Requesting…';
    }, 0);
  });

  // Back/forward restores this page from the cache with the button still disabled and
  // reading "Requesting...", which would leave someone looking at a form they can't
  // submit and no way to tell why.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    submitting = false;
    if (button) {
      button.disabled = false;
      button.textContent = 'Request';
    }
  });
})();
