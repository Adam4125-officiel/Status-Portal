// Typed confirmation for "Restore database", mirroring admin_update_control.js.
// Same reasoning, one step further: the update button replaces code and can be rolled
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
  var input = document.getElementById('restore-confirm-input');
  var totpInput = document.getElementById('restore-totp');
  var submitBtn = document.getElementById('restore-submit');
  var cancelBtn = document.getElementById('restore-cancel');
  var EXPECTED_WORD = 'REPLACE';

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
    var wordOk = input.value.trim().toUpperCase() === EXPECTED_WORD;
    var codeOk = !totpInput || totpInput.value.trim().length === 6;
    submitBtn.disabled = !(wordOk && codeOk);
  }

  fileInput.addEventListener('change', updateTriggerState);
  updateTriggerState();

  trigger.addEventListener('click', function () {
    // textContent, never innerHTML: the filename comes from the local filesystem, but
    // treating any non-constant string as markup is the habit that produced this
    // project's one real XSS (see CLAUDE.md on inline event-handler attributes).
    text.textContent = 'This replaces your entire database with "' + chosenName() +
      '" and restarts the portal. Your current database is saved first. Type "' +
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
