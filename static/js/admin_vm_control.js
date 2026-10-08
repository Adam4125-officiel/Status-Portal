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
  // admin_host_control.js / admin_system_control.js. VM control is step-up gated
  // (_require_totp()) exactly like host/app restart, so the panel asks for the 2FA code
  // (when 2FA is on) and a click on Confirm - there is deliberately no word to type as
  // well: the code is already the proof, and having to type START *and* a six-digit
  // code inside its thirty-second window was the annoyance this removed.
  var panel = document.getElementById('vm-control-confirm');
  if (!panel) return;
  var triggers = document.querySelectorAll('.vm-control-trigger');
  var text = document.getElementById('vm-control-confirm-text');
  var totpInput = document.getElementById('vm-control-totp');
  var nameField = document.getElementById('vm-control-name');
  var actionField = document.getElementById('vm-control-action');
  var submitBtn = document.getElementById('vm-control-submit');
  var cancelBtn = document.getElementById('vm-control-cancel');

  function updateSubmitState() {
    submitBtn.disabled = !!totpInput && totpInput.value.trim().length !== 6;
  }

  for (var i = 0; i < triggers.length; i++) {
    triggers[i].addEventListener('click', function (e) {
      var name = e.currentTarget.getAttribute('data-vm-name');
      var action = e.currentTarget.getAttribute('data-action');
      var label = e.currentTarget.getAttribute('data-label');
      nameField.value = name;
      actionField.value = action;
      text.textContent = label + ' VM ' + name + ' —' + (totpInput ? ' Enter your 2FA code to confirm.' : ' Press Confirm to go ahead.');
      if (totpInput) totpInput.value = '';
      updateSubmitState();
      panel.style.display = 'block';
      panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
      (totpInput || submitBtn).focus();
    });
  }

  if (totpInput) totpInput.addEventListener('input', updateSubmitState);

  cancelBtn.addEventListener('click', function () {
    panel.style.display = 'none';
  });
})();
