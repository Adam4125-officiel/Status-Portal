(function () {
  // Shows only the schedule field that the selected schedule kind actually uses.
  // Purely cosmetic: both inputs stay in the DOM and both still submit, so the
  // server never has to care which one was visible - same reasoning as the combined
  // wizard's collapsed "Advanced settings" block (see CLAUDE.md). Plain vanilla JS
  // with no dependency, like every other admin-side script in this app.
  var forms = document.querySelectorAll('form[data-task-schedule]');

  function apply(form) {
    var select = form.querySelector('[data-schedule-kind]');
    if (!select) return;
    var fields = form.querySelectorAll('[data-schedule-field]');
    for (var i = 0; i < fields.length; i++) {
      fields[i].style.display = fields[i].getAttribute('data-schedule-field') === select.value ? '' : 'none';
    }
  }

  for (var i = 0; i < forms.length; i++) {
    (function (form) {
      var select = form.querySelector('[data-schedule-kind]');
      if (select) select.addEventListener('change', function () { apply(form); });
      apply(form);
    })(forms[i]);
  }
})();
