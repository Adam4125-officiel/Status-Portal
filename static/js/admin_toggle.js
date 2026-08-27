// Submit-on-change for the .switch toggles in admin tables.
//
// The markup works without this file: each form carries a hidden field holding the
// value to switch to, plus a visible "Save" button. This script hides that button and
// submits on change instead, so the toggle behaves the way a toggle should - but a
// browser with JavaScript off still gets a working control rather than a dead one.
(function () {
  var forms = document.querySelectorAll('form.switch-form[data-autosubmit]');
  Array.prototype.forEach.call(forms, function (form) {
    var fallback = form.querySelector('.switch-form__fallback');
    if (fallback) fallback.style.display = 'none';
    var box = form.querySelector('.switch input[type="checkbox"]');
    if (!box) return;
    box.addEventListener('change', function () {
      // Disabled immediately so a double-click can't submit the same flip twice.
      box.disabled = true;
      form.submit();
    });
  });
})();
