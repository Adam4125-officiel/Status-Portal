(function () {
  // "Send these details to Seerr" asks first. The question is read from a data-confirm
  // attribute rather than written into an inline onsubmit="return confirm('...')",
  // because it names a Jellyfin user, which isn't ours to trust: the browser
  // HTML-decodes an inline handler and then parses it as JavaScript, so a quote in a
  // username broke out of the string. Same fix, same reasoning as the VM-name XSS in
  // admin_vm_control.js - here the value is only ever used as a string.
  var forms = document.querySelectorAll('form[data-confirm]');
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', function (e) {
      if (!confirm(e.currentTarget.getAttribute('data-confirm'))) e.preventDefault();
    });
  }
})();

(function () {
  // Brings this browser's stored theme into line with the preference that was just
  // saved on the account page.
  //
  // Without this the setting would appear to do nothing on the very device it was
  // changed from: a choice made with the floating toggle is stored in localStorage,
  // and localStorage deliberately outranks the account-level preference (it's the
  // more recent deliberate action *on this device*). So saving "Light" here while
  // this browser has "dark" stored locally would change every other device and
  // visibly not this one - the most confusing possible outcome.
  //
  // Only runs immediately after a save (the page renders data-just-saved then), never
  // on an ordinary visit, so it can't quietly undo a local toggle at some later point.
  var root = document.getElementById('account-prefs');
  if (!root || root.getAttribute('data-just-saved') !== '1') return;

  var theme = root.getAttribute('data-theme-pref');
  try {
    if (theme === 'auto') {
      // Back to following this device's OS setting: forget the local override too,
      // otherwise "Auto" would keep whatever was last toggled here forever.
      localStorage.removeItem('portal-theme');
    } else {
      localStorage.setItem('portal-theme', theme);
    }
  } catch (e) { /* storage unavailable (private mode) - the server preference still applies */ }

  var osPrefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  var effective = theme === 'auto' ? (osPrefersLight ? 'light' : 'dark') : theme;
  if (effective === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = effective === 'light' ? '🌙' : '☀️';
})();
