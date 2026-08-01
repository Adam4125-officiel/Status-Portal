(function () {
  // Injects the per-session CSRF token (rendered into a <meta> tag by base.html)
  // as a hidden field into every POST form on the page, rather than hand-editing
  // every template that has one - avoids the real risk of missing a spot across
  // the ~16 templates with a <form method="POST">. See app.py's _check_csrf() for
  // the server-side check this token satisfies.
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta || !meta.content) return;
  var token = meta.content;

  var forms = document.querySelectorAll('form');
  for (var i = 0; i < forms.length; i++) {
    var form = forms[i];
    if ((form.method || '').toLowerCase() !== 'post') continue;
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = token;
    form.appendChild(input);
  }
})();
