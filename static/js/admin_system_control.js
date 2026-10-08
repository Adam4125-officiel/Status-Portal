(function () {
  var panel = document.getElementById('system-control-confirm');
  if (!panel) return;
  var triggers = document.querySelectorAll('.system-control-trigger');
  var text = document.getElementById('system-control-confirm-text');
  var totpInput = document.getElementById('system-control-totp');
  var componentField = document.getElementById('system-control-component');
  var submitBtn = document.getElementById('system-control-submit');
  var cancelBtn = document.getElementById('system-control-cancel');

  function updateSubmitState() {
    submitBtn.disabled = !!totpInput && totpInput.value.trim().length !== 6;
  }

  for (var i = 0; i < triggers.length; i++) {
    triggers[i].addEventListener('click', function (e) {
      var component = e.currentTarget.getAttribute('data-component');
      var label = e.currentTarget.getAttribute('data-label');
      componentField.value = component;
      text.textContent = label + ' —' + (totpInput ? ' Enter your 2FA code to confirm.' : ' Press Confirm to go ahead.');
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
