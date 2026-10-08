// The service forms' "Group / category" control: a list of the existing groups, plus a way
// to create a new one. The text box (name="group_name") is what the form submits either way;
// this script only decides what the person sees. Without it the box is simply always visible.
(function () {
  var NEW_GROUP = '__new__';

  function setup(root) {
    var select = root.querySelector('.group-picker__select');
    var input = root.querySelector('input[name="group_name"]');
    if (!select || !input) return;

    function showBox(visible) {
      input.hidden = !visible;
      // Only a box the person can see may block the submit: "New group..." chosen and left
      // empty would otherwise quietly save the service with no group at all.
      input.required = visible;
    }

    select.hidden = false;
    var current = input.value.trim();
    var known = false;
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === current && current !== NEW_GROUP) known = true;
    }
    if (current === '' || known) {
      select.value = current;
      showBox(false);
    } else {
      // A name the list doesn't have (nothing else uses it any more): keep it, editable.
      select.value = NEW_GROUP;
      showBox(true);
    }

    select.addEventListener('change', function () {
      if (select.value === NEW_GROUP) {
        input.value = '';
        showBox(true);
        input.focus();
      } else {
        input.value = select.value;
        showBox(false);
      }
    });
  }

  var roots = document.querySelectorAll('[data-group-picker]');
  for (var r = 0; r < roots.length; r++) setup(roots[r]);
})();
