// The kiosk display's rotation, auto-scroll, data refresh and cursor hiding.
//
// Four concerns, on three clocks:
//
//   * rotation   - moves to the next view every kiosk_rotation_seconds.
//   * autoscroll - within a view that doesn't fit, scrolls to the bottom and back over
//                  that same slot, so a small screen shows all of a long list instead
//                  of its top third.
//   * refresh    - re-fetches every view from /kiosk/views on the ordinary public-page
//                  refresh cycle and swaps them in, *without* reloading the page.
//                  main.js's window.location.reload() would flash the screen and throw
//                  the rotation back to the first view every cycle, so with a 60s
//                  refresh and a 20s rotation the later views would never be reached.
//   * cursor     - hides the pointer after a few idle seconds (see below).
//
// The first two (and the progress bar) share one requestAnimationFrame loop reading one
// timestamp, deliberately: they are three views of the same question - how far through
// this slot are we - and on separate timers they could answer it differently. The
// refresh and the clock are genuinely independent and keep their own intervals.
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

  // How the view's slot is spent: a pause at the top to read the first rows, a scroll
  // to the bottom, a pause there, then back to the top - so the view is where it
  // started when the rotation comes round to it again. The journey back up is whatever
  // is left (1 - the three below), so these must stay under 1 between them.
  var SCROLL_HOLD_TOP = 0.15;
  var SCROLL_DOWN = 0.35;
  var SCROLL_HOLD_BOTTOM = 0.15;
  // Below this, an overflow is a stray pixel or two of rounding and scrolling it would
  // just make the view twitch.
  var SCROLL_MIN_OVERFLOW_PX = 24;
  var reducedMotion = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var views = [];
  var index = 0;
  var viewStartedAt = performance.now();
  // Set when somebody actually scrolls the view themselves, and cleared by the next
  // rotation: whoever walked up to the display wants to read what they scrolled to,
  // not wrestle a timer for control of the scrollbar.
  var scrollTakenOver = false;

  function elapsedMs() {
    return performance.now() - viewStartedAt;
  }

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
    startSlot();
    renderDots();
  }

  function startSlot() {
    viewStartedAt = performance.now();
    scrollTakenOver = false;
  }

  function advance() {
    // With only one view there is nothing to advance to, but its slot still restarts -
    // that is what makes a single long view scroll down and back round again forever
    // rather than sitting at the bottom once it has got there.
    if (views.length > 1) show(index + 1);
    else startSlot();
  }

  // ---- auto-scroll --------------------------------------------------------
  // Only ever moves a view that genuinely doesn't fit, which is self-gating: a 1080p
  // television showing six services has no overflow and never scrolls, while the same
  // page on a 7" tablet does. That's why there's no breakpoint here.
  function scrollFraction(progress) {
    var downEnds = SCROLL_HOLD_TOP + SCROLL_DOWN;
    var upStarts = downEnds + SCROLL_HOLD_BOTTOM;
    if (progress <= SCROLL_HOLD_TOP) return 0;
    if (progress >= 1) return 0;
    if (progress < downEnds) return ease((progress - SCROLL_HOLD_TOP) / SCROLL_DOWN);
    if (progress < upStarts) return 1;
    return 1 - ease((progress - upStarts) / (1 - upStarts));
  }

  // Smoothstep, so the scroll eases in and out instead of starting and stopping dead -
  // a linear ramp on a wall display reads as a machine dragging the page.
  function ease(x) {
    var t = Math.max(0, Math.min(1, x));
    return t * t * (3 - 2 * t);
  }

  function autoScroll(rawProgress) {
    if (!views.length || scrollTakenOver) return;
    var body = views[index].querySelector('.kiosk-view__body');
    if (!body) return;
    // Measured every frame rather than once per slot: a refresh can swap the view's
    // contents underneath it, and web fonts landing after first paint change the
    // height of everything.
    var overflow = body.scrollHeight - body.clientHeight;
    if (overflow <= SCROLL_MIN_OVERFLOW_PX) return;
    var progress = Math.min(1, rawProgress);
    if (reducedMotion) {
      // Someone who has asked their system for less motion gets the content in two
      // cuts rather than a continuous scroll - the standard accessible substitute for
      // a scroll animation. Not simply "no scrolling": that would leave the bottom of
      // a long list permanently unreachable on the screen it matters most on.
      body.scrollTop = (progress > SCROLL_HOLD_TOP + SCROLL_DOWN
        && progress < SCROLL_HOLD_TOP + SCROLL_DOWN + SCROLL_HOLD_BOTTOM) ? overflow : 0;
    } else {
      body.scrollTop = overflow * scrollFraction(progress);
    }
    // Read back rather than storing what we asked for: the browser clamps and rounds,
    // and the comparison in the scroll handler has to be against what actually landed.
    body.__kioskScrollTop = body.scrollTop;
  }

  // A manual scroll hands control over for the rest of the slot. Checked against the
  // position the animation last set rather than on the event alone, because assigning
  // scrollTop above fires 'scroll' too and would otherwise switch itself off instantly.
  container.addEventListener('scroll', function (event) {
    var body = event.target;
    if (!body.classList || !body.classList.contains('kiosk-view__body')) return;
    if (Math.abs(body.scrollTop - (body.__kioskScrollTop || 0)) > 2) scrollTakenOver = true;
  }, true);
  ['wheel', 'touchstart'].forEach(function (event) {
    container.addEventListener(event, function () { scrollTakenOver = true; }, { passive: true });
  });

  // ---- data refresh -------------------------------------------------------
  function applyFragment(html) {
    // Which view is on screen has to survive the swap, or every refresh would jump
    // the display back to the first one - the exact problem a full page reload has.
    var currentKey = views.length ? views[index].getAttribute('data-view') : null;
    var spent = elapsedMs();
    var wasTakenOver = scrollTakenOver;

    container.innerHTML = html;
    collectViews();

    var restored = 0;
    views.forEach(function (view, i) {
      if (view.getAttribute('data-view') === currentKey) restored = i;
    });
    show(restored);
    // show() restarts the slot; the swap shouldn't have bought the current view a
    // fresh full turn on screen, nor wrestled the scroll back off somebody who had
    // taken it over mid-slot, so both are put back.
    viewStartedAt = performance.now() - spent;
    scrollTakenOver = wasTakenOver;

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

  // Rotation, the progress bar and the auto-scroll all read the same clock on the same
  // frame, so they cannot disagree about how far through the slot the display is.
  //
  // Rotation is checked here rather than on a one-second timer, which it used to be:
  // a 1s tick can only notice that 20 seconds have passed at the first tick *after*
  // they have, so every slot ran 20 to 21 seconds and "20 seconds per view" quietly
  // meant something else. Elapsed time is a timestamp now, so there is no reason to
  // keep the coarse check.
  //
  // rAF stops while the tab is hidden, which for a display that is never backgrounded
  // changes nothing - and if one is, not burning through views nobody is looking at is
  // the better behaviour anyway. The data refresh below is on its own timer and is
  // unaffected.
  function frame() {
    requestAnimationFrame(frame);
    var progress = elapsedMs() / (rotationSeconds * 1000);
    if (progressEl) {
      progressEl.style.width = views.length > 1 ? Math.min(100, progress * 100) + '%' : '0%';
    }
    autoScroll(progress);
    if (progress >= 1) advance();
  }
  requestAnimationFrame(frame);

  setInterval(tickClock, 1000);
  setInterval(refresh, refreshSeconds * 1000);
})();
