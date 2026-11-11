(function () {
  // A form with a data-confirm attribute asks first (admin delete buttons, "Send these
  // details to Seerr" on the account page). The question is read from a data-confirm
  // attribute rather than written into an inline onsubmit="return confirm('...')",
  // because it names a Jellyfin user, which isn't ours to trust: the browser
  // HTML-decodes an inline handler and then parses it as JavaScript, so a quote in a
  // username broke out of the string. Same fix, same reasoning as the VM-name XSS in
  // admin_vm_control.js - here the value is only ever used as a string.
  var forms = document.querySelectorAll('form[data-confirm]');
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', function (e) {
      if (!confirm(e.currentTarget.getAttribute('data-confirm'))) e.preventDefault();
    });
  }
})();
