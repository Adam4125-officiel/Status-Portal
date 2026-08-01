(function () {
  var panel = document.getElementById('host-control-confirm');
  if (!panel) return;
  var triggers = document.querySelectorAll('.host-control-trigger');
  var text = document.getElementById('host-control-confirm-text');
  var input = document.getElementById('host-control-confirm-input');
  var totpInput = document.getElementById('host-control-totp');
  var actionField = document.getElementById('host-control-action');
  var submitBtn = document.getElementById('host-control-submit');
  var cancelBtn = document.getElementById('host-control-cancel');
  var expectedWord = '';

  function updateSubmitState() {
    var wordOk = input.value.trim().toUpperCase() === expectedWord;
    var codeOk = !totpInput || totpInput.value.trim().length === 6;
    submitBtn.disabled = !(wordOk && codeOk);
  }

  for (var i = 0; i < triggers.length; i++) {
    triggers[i].addEventListener('click', function (e) {
      var action = e.currentTarget.getAttribute('data-action');
      var label = e.currentTarget.getAttribute('data-label');
      expectedWord = action.toUpperCase();
      actionField.value = action;
      text.textContent = label + ' — this cannot be undone from here. ' +
        'Type "' + expectedWord + '" below to confirm.';
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
