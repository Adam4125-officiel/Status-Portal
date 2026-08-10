// Typed confirmation for the "Update now" button, mirroring
// admin_system_control.js / admin_host_control.js. Same reasoning: this action
// installs and then runs new code, so it should not be a single stray click.
(function () {
  var trigger = document.getElementById('update-trigger');
  var panel = document.getElementById('update-confirm');
  if (!trigger || !panel) return;
  var text = document.getElementById('update-confirm-text');
  var input = document.getElementById('update-confirm-input');
  var totpInput = document.getElementById('update-totp');
  var submitBtn = document.getElementById('update-submit');
  var cancelBtn = document.getElementById('update-cancel');
  var EXPECTED_WORD = 'UPDATE';

  function updateSubmitState() {
    var wordOk = input.value.trim().toUpperCase() === EXPECTED_WORD;
    var codeOk = !totpInput || totpInput.value.trim().length === 6;
    submitBtn.disabled = !(wordOk && codeOk);
  }

  trigger.addEventListener('click', function () {
    text.textContent = 'This installs new code and restarts the portal — type "' +
      EXPECTED_WORD + '" below to confirm.';
    input.value = '';
    if (totpInput) totpInput.value = '';
    updateSubmitState();
    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    input.focus();
  });

  input.addEventListener('input', updateSubmitState);
  if (totpInput) totpInput.addEventListener('input', updateSubmitState);

  cancelBtn.addEventListener('click', function () {
    panel.style.display = 'none';
  });
})();
