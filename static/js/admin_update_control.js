// Confirmation panel for the "Update now" button, mirroring
// admin_system_control.js / admin_host_control.js. This action installs and then runs new
// code, so it is not a single stray click: the button opens a panel, which asks for the
// 2FA code (when 2FA is on) and a click on Confirm.
(function () {
  var trigger = document.getElementById('update-trigger');
  var panel = document.getElementById('update-confirm');
  if (!trigger || !panel) return;
  var text = document.getElementById('update-confirm-text');
  var totpInput = document.getElementById('update-totp');
  var submitBtn = document.getElementById('update-submit');
  var cancelBtn = document.getElementById('update-cancel');

  function updateSubmitState() {
    submitBtn.disabled = !!totpInput && totpInput.value.trim().length !== 6;
  }

  trigger.addEventListener('click', function () {
    text.textContent = 'This installs new code and restarts the portal.' + (totpInput ? ' Enter your 2FA code to confirm.' : ' Press Confirm to go ahead.');
    if (totpInput) totpInput.value = '';
    updateSubmitState();
    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    (totpInput || submitBtn).focus();
  });

  if (totpInput) totpInput.addEventListener('input', updateSubmitState);

  cancelBtn.addEventListener('click', function () {
    panel.style.display = 'none';
  });
})();
