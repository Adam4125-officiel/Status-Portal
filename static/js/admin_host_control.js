(function () {
  var panel = document.getElementById('host-control-confirm');
  if (!panel) return;
  var triggers = document.querySelectorAll('.host-control-trigger');
  var text = document.getElementById('host-control-confirm-text');
  var totpInput = document.getElementById('host-control-totp');
  var actionField = document.getElementById('host-control-action');
  var submitBtn = document.getElementById('host-control-submit');
  var cancelBtn = document.getElementById('host-control-cancel');

  function updateSubmitState() {
    submitBtn.disabled = !!totpInput && totpInput.value.trim().length !== 6;
  }

  for (var i = 0; i < triggers.length; i++) {
    triggers[i].addEventListener('click', function (e) {
      var action = e.currentTarget.getAttribute('data-action');
      var label = e.currentTarget.getAttribute('data-label');
      actionField.value = action;
      text.textContent = label + ' — this cannot be undone from here.' + (totpInput ? ' Enter your 2FA code to confirm.' : ' Press Confirm to go ahead.');
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
