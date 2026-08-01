(function () {
  var list = document.getElementById('layout-order-list');
  var hidden = document.getElementById('layout_order_input');
  if (!list || !hidden) return;

  function updateHidden() {
    var items = list.querySelectorAll('li');
    var keys = [];
    for (var i = 0; i < items.length; i++) keys.push(items[i].getAttribute('data-key'));
    hidden.value = keys.join(',');
  }

  list.addEventListener('click', function (e) {
    var btn = e.target.closest('button');
    if (!btn) return;
    e.preventDefault();
    var li = btn.closest('li');
    if (!li) return;
    if (btn.classList.contains('move-up') && li.previousElementSibling) {
      list.insertBefore(li, li.previousElementSibling);
    } else if (btn.classList.contains('move-down') && li.nextElementSibling) {
      list.insertBefore(li.nextElementSibling, li);
    }
    updateHidden();
  });
})();
