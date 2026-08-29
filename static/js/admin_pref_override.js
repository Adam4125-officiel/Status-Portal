(function () {
  // Same reasoning as admin_vm_control.js: read the confirmation text from a plain
  // data-* attribute rather than an inline onsubmit="confirm('...' + label + '...')" -
  // it's used only as a JS string here, never re-inserted into HTML or re-evaluated
  // as code.
  var forms = document.querySelectorAll('.notification-override-form');
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', function (e) {
      var label = e.currentTarget.getAttribute('data-label');
      var value = e.currentTarget.getAttribute('data-value');
      if (!confirm('Set "' + label + '" = ' + value + ' for every existing user, ' +
                    'overwriting their own individual choice? This cannot be undone.')) {
        e.preventDefault();
      }
    });
  }
})();
