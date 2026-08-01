(function () {
  // Confirmation is attached here (reading plain data-* attributes) rather than an
  // inline onsubmit="confirm('...' + vm.name + '...')" - a VM name comes from
  // Hyper-V itself, not necessarily from someone who already has portal-admin
  // credentials (e.g. anyone able to create/rename a VM on the host), so it must
  // never be interpolated into a string that gets re-parsed as JS. Reading it as a
  // plain attribute value and using it only as a JS string (never re-inserted into
  // HTML or re-evaluated as code) avoids that class of injection entirely.
  var forms = document.querySelectorAll('.vm-control-form');
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', function (e) {
      var label = e.currentTarget.getAttribute('data-label');
      var name = e.currentTarget.getAttribute('data-vm-name');
      if (!confirm(label + ' VM ' + name + '?')) {
        e.preventDefault();
      }
    });
  }
})();
