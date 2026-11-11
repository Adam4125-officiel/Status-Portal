// Submit-on-change for <select data-autosubmit> controls on admin pages.
//
// requestSubmit(), not submit(): form.submit() bypasses the submit event, so
// admin_scroll_restore.js would never hear about it and changing a row far down a
// page would jump back to the top.
(function () {
  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches('select[data-autosubmit]') || !el.form) return;
    if (el.form.requestSubmit) el.form.requestSubmit();
    else el.form.submit();
  });
})();
