// Dismissal for the flash messages, which are pinned to the top of the viewport so
// they stay visible now that a save keeps your scroll position (see
// admin_scroll_restore.js - restoring the scroll would otherwise leave "Settings
// saved" sitting off-screen at the top of a long page, i.e. an action with no
// visible confirmation at all).
//
// Auto-dismissal is an enhancement, not a requirement: with JavaScript off the
// messages simply stay until the next page load, which is what they did before.
(function () {
  var TIMEOUT_MS = 7000;
  var flashes = document.querySelectorAll('.flash-stack .flash');
  Array.prototype.forEach.call(flashes, function (flash) {
    function dismiss() {
      flash.classList.add('flash--leaving');
      // Removed rather than just faded, so it stops covering anything underneath.
      setTimeout(function () { flash.remove(); }, 200);
    }
    flash.addEventListener('click', dismiss);
    // Errors stay put: they often name something that needs reading twice, and an
    // error that vanishes while you are still working out what it meant is worse
    // than one you have to click away.
    if (!flash.classList.contains('error')) setTimeout(dismiss, TIMEOUT_MS);
  });
})();
