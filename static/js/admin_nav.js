// Collapses the admin menu on narrow screens and keeps it open on wide ones.
//
// The menu is rendered open so it works without JavaScript. Below 720px this closes it
// (the summary then shows the current page and the badge total); at 720px and above the
// summary is hidden by CSS, so the menu is forced open again if the window is resized.
(function () {
  var menu = document.getElementById('admin_nav_menu');
  if (!menu || !window.matchMedia) return;
  var narrow = window.matchMedia('(max-width: 720px)');
  function sync() { menu.open = !narrow.matches; }
  sync();
  if (narrow.addEventListener) narrow.addEventListener('change', sync);
  else if (narrow.addListener) narrow.addListener(sync);
})();
