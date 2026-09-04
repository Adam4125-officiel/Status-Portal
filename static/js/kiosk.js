// The kiosk display's rotation, data refresh and cursor hiding.
//
// Three timers, and the reason they're separate matters:
//
//   * rotation  - moves to the next view every kiosk_rotation_seconds.
//   * refresh   - re-fetches every view from /kiosk/views on the ordinary public-page
//                 refresh cycle and swaps them in, *without* reloading the page.
//                 main.js's window.location.reload() would flash the screen and throw
//                 the rotation back to the first view every cycle, so with a 60s
//                 refresh and a 20s rotation the later views would never be reached.
//   * cursor    - hides the pointer after a few idle seconds (see below).
//
// The refresh deliberately keeps the currently-showing view showing: what changes is
// the data inside it, not where the rotation had got to.
(function () {
  var screenEl = document.querySelector('.kiosk-screen');
  if (!screenEl) return;

  var container = document.getElementById('kiosk-views');
  var dotsEl = document.getElementById('kiosk-dots');
  var clockEl = document.getElementById('kiosk-clock');
  var staleEl = document.getElementById('kiosk-stale');
  var progressEl = document.getElementById('kiosk-progress');

  var rotationSeconds = parseInt(screenEl.getAttribute('data-rotation-seconds'), 10) || 20;
  var refreshSeconds = parseInt(screenEl.getAttribute('data-refresh-seconds'), 10) || 60;
  var viewsUrl = screenEl.getAttribute('data-views-url');

  // How many consecutive failed polls before the display admits it. One is a blip
  // (a restart, a dropped wifi frame); a display that cried wolf on every one of
  // those would train whoever walks past to ignore the warning that matters.
  var STALE_AFTER_FAILURES = 2;
  var failures = 0;

  var views = [];
  var index = 0;
  var elapsed = 0;

  function collectViews() {
    views = Array.prototype.slice.call(container.querySelectorAll('.kiosk-view'));
    // The fragment carries the current rotation interval, so changing it in the admin
    // panel takes effect on the next refresh rather than needing somebody to go and
    // reload a display that is mounted on a wall.
    var wrapper = container.querySelector('.kiosk-views[data-rotation-seconds]');
    var fromFragment = wrapper && parseInt(wrapper.getAttribute('data-rotation-seconds'), 10);
    if (fromFragment) rotationSeconds = fromFragment;
  }

  function renderDots() {
    if (!dotsEl) return;
    dotsEl.textContent = '';
    // A single view isn't a rotation, so it gets no indicator at all rather than one
    // permanently-lit dot that looks like something is stuck.
    if (views.length < 2) return;
    views.forEach(function (view, i) {
      var dot = document.createElement('span');
      dot.className = 'kiosk-dot' + (i === index ? ' kiosk-dot--on' : '');
      dotsEl.appendChild(dot);
    });
  }

  function show(next) {
    if (!views.length) return;
    index = ((next % views.length) + views.length) % views.length;
    views.forEach(function (view, i) {
      view.classList.toggle('kiosk-view--on', i === index);
    });
    elapsed = 0;
    renderDots();
  }

  function advance() {
    if (views.length > 1) show(index + 1);
    elapsed = 0;
  }

  // ---- data refresh -------------------------------------------------------
  function applyFragment(html) {
    // Which view is on screen has to survive the swap, or every refresh would jump
    // the display back to the first one - the exact problem a full page reload has.
    var currentKey = views.length ? views[index].getAttribute('data-view') : null;
    var remaining = elapsed;

    container.innerHTML = html;
    collectViews();

    var restored = 0;
    views.forEach(function (view, i) {
      if (view.getAttribute('data-view') === currentKey) restored = i;
    });
    show(restored);
    // show() resets the rotation clock; the swap shouldn't have bought the current
    // view a fresh full turn on screen, so put the elapsed time back.
    elapsed = remaining;

    // These timestamps arrived after the page's load event, so local_time.js has
    // already run over the document and won't see them on its own.
    if (window.applyLocalTimes) window.applyLocalTimes(container);
  }

  function refresh() {
    fetch(viewsUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
      })
      .then(function (html) {
        applyFragment(html);
        failures = 0;
        if (staleEl) staleEl.hidden = true;
      })
      .catch(function () {
        failures += 1;
        if (staleEl && failures >= STALE_AFTER_FAILURES) staleEl.hidden = false;
      });
  }

  // ---- clock --------------------------------------------------------------
  function tickClock() {
    if (!clockEl) return;
    clockEl.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  // ---- cursor -------------------------------------------------------------
  // Hidden after a few idle seconds rather than always (`cursor: none` outright would
  // be simpler, and would also make the display impossible to interact with when
  // somebody does walk up to it with a mouse or a touchscreen).
  var CURSOR_IDLE_SECONDS = 3;
  var cursorTimer = null;
  function wakeCursor() {
    screenEl.classList.remove('kiosk-screen--nocursor');
    if (cursorTimer) clearTimeout(cursorTimer);
    cursorTimer = setTimeout(function () {
      screenEl.classList.add('kiosk-screen--nocursor');
    }, CURSOR_IDLE_SECONDS * 1000);
  }
  ['mousemove', 'mousedown', 'touchstart', 'keydown'].forEach(function (event) {
    document.addEventListener(event, wakeCursor, { passive: true });
  });

  // ---- wiring -------------------------------------------------------------
  collectViews();
  show(0);
  tickClock();
  wakeCursor();

  // One second-resolution timer drives the rotation and the progress bar rather than
  // a setTimeout per view, so a rotation interval saved in the admin panel while the
  // display is running is picked up on the next poll instead of the next restart.
  setInterval(function () {
    elapsed += 1;
    if (progressEl) {
      progressEl.style.width = views.length > 1
        ? Math.min(100, (elapsed / rotationSeconds) * 100) + '%'
        : '0%';
    }
    if (elapsed >= rotationSeconds) advance();
    tickClock();
  }, 1000);

  setInterval(refresh, refreshSeconds * 1000);
})();
