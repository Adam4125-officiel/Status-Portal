(function () {
  var panel = document.getElementById('system-control-confirm');
  if (!panel) return;
  var triggers = document.querySelectorAll('.system-control-trigger');
  var text = document.getElementById('system-control-confirm-text');
  var input = document.getElementById('system-control-confirm-input');
  var totpInput = document.getElementById('system-control-totp');
  var componentField = document.getElementById('system-control-component');
  var submitBtn = document.getElementById('system-control-submit');
  var cancelBtn = document.getElementById('system-control-cancel');
  var EXPECTED_WORD = 'RESTART';

  function updateSubmitState() {
    var wordOk = input.value.trim().toUpperCase() === EXPECTED_WORD;
    var codeOk = !totpInput || totpInput.value.trim().length === 6;
    submitBtn.disabled = !(wordOk && codeOk);
  }

  for (var i = 0; i < triggers.length; i++) {
    triggers[i].addEventListener('click', function (e) {
      var component = e.currentTarget.getAttribute('data-component');
      var label = e.currentTarget.getAttribute('data-label');
      componentField.value = component;
      text.textContent = label + ' — type "' + EXPECTED_WORD + '" below to confirm.';
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
