(function () {
  var rows = document.getElementById('links-rows');
  var addBtn = document.getElementById('add-link');
  if (!rows || !addBtn) return;

  function makeRow() {
    var row = document.createElement('div');
    row.className = 'link-row';
    row.style.display = 'flex';
    row.style.gap = '8px';

    var label = document.createElement('input');
    label.type = 'text';
    label.name = 'link_label';
    label.placeholder = 'Label (e.g. Tailscale)';
    label.style.flex = '1';

    var url = document.createElement('input');
    url.type = 'url';
    url.name = 'link_url';
    url.placeholder = 'https://...';
    url.style.flex = '2';

    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn secondary link-remove';
    remove.textContent = '✕';

    row.appendChild(label);
    row.appendChild(url);
    row.appendChild(remove);
    return row;
  }

  addBtn.addEventListener('click', function () {
    rows.appendChild(makeRow());
  });

  rows.addEventListener('click', function (e) {
    if (e.target.classList.contains('link-remove')) {
      e.target.closest('.link-row').remove();
    }
  });
})();
