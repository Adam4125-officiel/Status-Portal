// Confirmation panel for "Restore database", mirroring admin_update_control.js.
// One step further than the update: the update button replaces code and can be rolled
// back from a shell, whereas this replaces every piece of data the portal holds. The
// button additionally stays disabled until a file has actually been chosen, so the
// confirmation can name the file being restored rather than asking the admin to
// confirm something abstract.
(function () {
  var trigger = document.getElementById('restore-trigger');
  var panel = document.getElementById('restore-confirm');
  if (!trigger || !panel) return;
  var fileInput = document.getElementById('restore-file');
  var text = document.getElementById('restore-confirm-text');
  var totpInput = document.getElementById('restore-totp');
  var submitBtn = document.getElementById('restore-submit');
  var cancelBtn = document.getElementById('restore-cancel');

  function chosenName() {
    return fileInput.files && fileInput.files.length ? fileInput.files[0].name : '';
  }

  function updateTriggerState() {
    trigger.disabled = !chosenName();
    // Choosing a different file after opening the panel would otherwise leave the
    // confirmation naming the previous one.
    panel.style.display = 'none';
  }

  function updateSubmitState() {
    submitBtn.disabled = !!totpInput && totpInput.value.trim().length !== 6;
  }

  fileInput.addEventListener('change', updateTriggerState);
  updateTriggerState();

  trigger.addEventListener('click', function () {
    // textContent, never innerHTML: the filename comes from the local filesystem, but
    // treating any non-constant string as markup is the habit that produced this
    // project's one real XSS (see CLAUDE.md on inline event-handler attributes).
    text.textContent = 'This replaces your entire database with "' + chosenName() +
      '" and restarts the portal. Your current database is saved first.' + (totpInput ? ' Enter your 2FA code to confirm.' : ' Press Confirm to go ahead.');
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
