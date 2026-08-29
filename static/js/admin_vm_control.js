(function () {
  // Confirmation is attached here (reading plain data-* attributes) rather than an
  // inline onsubmit="confirm('...' + vm.name + '...')" - a VM name comes from
  // Hyper-V itself, not necessarily from someone who already has portal-admin
  // credentials (e.g. anyone able to create/rename a VM on the host), so it must
  // never be interpolated into a string that gets re-parsed as JS. Reading it as a
  // plain attribute value and using it only as a JS string (never re-inserted into
  // HTML or re-evaluated as code) avoids that class of injection entirely.
  //
  // One shared confirm panel driving every trigger button, same shape as
  // admin_host_control.js / admin_system_control.js - VM control is step-up
  // gated (_require_totp()) exactly like host/app restart, so it needs the same
  // typed-confirmation + optional code field, not the plain browser confirm()
  // this used before.
  var panel = document.getElementById('vm-control-confirm');
  if (!panel) return;
  var triggers = document.querySelectorAll('.vm-control-trigger');
  var text = document.getElementById('vm-control-confirm-text');
  var input = document.getElementById('vm-control-confirm-input');
  var totpInput = document.getElementById('vm-control-totp');
  var nameField = document.getElementById('vm-control-name');
  var actionField = document.getElementById('vm-control-action');
  var submitBtn = document.getElementById('vm-control-submit');
  var cancelBtn = document.getElementById('vm-control-cancel');
  var expectedWord = '';

  function updateSubmitState() {
    var wordOk = input.value.trim().toUpperCase() === expectedWord;
    var codeOk = !totpInput || totpInput.value.trim().length === 6;
    submitBtn.disabled = !(wordOk && codeOk);
  }

  for (var i = 0; i < triggers.length; i++) {
    triggers[i].addEventListener('click', function (e) {
      var name = e.currentTarget.getAttribute('data-vm-name');
      var action = e.currentTarget.getAttribute('data-action');
      var label = e.currentTarget.getAttribute('data-label');
      expectedWord = action.toUpperCase();
      nameField.value = name;
      actionField.value = action;
      text.textContent = label + ' VM ' + name + ' — type "' + expectedWord + '" below to confirm.';
      input.value = '';
      if (totpInput) totpInput.value = '';
      updateSubmitState();
      panel.style.display = 'block';
      panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
      input.focus();
    });
  }

  input.addEventListener('input', updateSubmitState);
  if (totpInput) totpInput.addEventListener('input', updateSubmitState);

  cancelBtn.addEventListener('click', function () {
    panel.style.display = 'none';
  });
})();
