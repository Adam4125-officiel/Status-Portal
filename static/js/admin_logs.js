// Starts the log view at the newest entry.
//
// The entries are rendered oldest-first, because that is the order a log reads in
// and a traceback has to be read downwards - but the reason you open this page is
// almost always "what just happened", which is at the bottom. So the box is scrolled
// to the end on load rather than making you drag to it every time.
(function () {
  var view = document.querySelector('.log-view');
  if (!view) return;
  view.scrollTop = view.scrollHeight;
})();
