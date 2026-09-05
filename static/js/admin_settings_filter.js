(function () {
  // Filters the Settings page in place. It is one long page of ~35 separate settings
  // across three forms, and finding one meant scrolling and reading every label.
  //
  // Two properties that matter and are easy to break:
  //
  // - **Hiding is visual only.** A hidden field's inputs are still in the DOM and
  //   still submit, exactly like the combined wizard's collapsed <details> block. So
  //   saving while a filter is active saves the whole form, not just what's on
  //   screen - nothing is silently reset. Never "improve" this by removing or
  //   disabling the hidden inputs.
  // - **The unit is a top-level .field.** Some fields nest others (the kiosk block
  //   holds its own rotation-seconds field), and hiding a nested one on its own would
  //   leave a section looking broken. Matching a parent shows everything inside it.
  var wrap = document.getElementById('settings_filter');
  var input = document.getElementById('settings_filter_input');
  var clear = document.getElementById('settings_filter_clear');
  var status = document.getElementById('settings_filter_status');
  if (!wrap || !input || !clear || !status) return;

  var panels = Array.prototype.slice.call(document.querySelectorAll('.form-panel'));
  if (!panels.length) return;

  // One entry per searchable block: the element to show/hide, its panel, and the text
  // to match against. Built once - the page is static after load.
  var blocks = [];
  panels.forEach(function (panel) {
    Array.prototype.slice.call(panel.children).forEach(function (child) {
      if (!child.classList.contains('field')) return;
      blocks.push({
        el: child,
        panel: panel,
        // Label text, hints and the option labels inside a checkbox list all count -
        // someone searching "trickplay" or "Prowlarr" is searching for words that only
        // appear in a hint or a checkbox, never in a heading.
        text: (child.textContent || '').toLowerCase() + ' ' + inputNames(child),
      });
    });
  });

  function inputNames(el) {
    // Field *names* are searchable too, so "show_public_gpu" from a log line or a
    // .env example finds its setting without knowing what it's called in English.
    return Array.prototype.slice.call(el.querySelectorAll('[name]'))
      .map(function (n) { return n.getAttribute('name'); })
      .join(' ')
      .toLowerCase();
  }

  function apply(term) {
    term = term.trim().toLowerCase();
    var shown = 0;
    blocks.forEach(function (block) {
      var match = !term || block.text.indexOf(term) !== -1;
      block.el.hidden = !match;
      if (match) shown++;
    });
    // A panel whose every field is hidden goes too, so its Save button doesn't sit
    // alone under a heading with nothing above it.
    panels.forEach(function (panel) {
      var any = blocks.some(function (b) { return b.panel === panel && !b.el.hidden; });
      panel.hidden = Boolean(term) && !any;
    });

    if (!term) {
      status.hidden = true;
      status.textContent = '';
      return;
    }
    status.hidden = false;
    status.textContent = shown
      ? shown + ' of ' + blocks.length + ' settings shown.'
      : 'Nothing matches “' + term + '”. Some settings live on other pages — try '
        + 'Notifications, Discord bot, Scheduled tasks or About.';
  }

  input.addEventListener('input', function () { apply(input.value); });
  input.addEventListener('keydown', function (e) {
    // Escape clears rather than blurring, which is what a filter box should do - the
    // page comes back in one keystroke.
    if (e.key === 'Escape' && input.value) {
      e.preventDefault();
      input.value = '';
      apply('');
    }
  });
  clear.addEventListener('click', function () {
    input.value = '';
    apply('');
    input.focus();
  });

  wrap.hidden = false;
})();
