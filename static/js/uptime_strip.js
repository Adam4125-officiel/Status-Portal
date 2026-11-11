// Reading the 90-day strip on a public card without a hover.
//
// Each bar carries its day and figure in data-label (a title tooltip covers the mouse).
// Touch: press or drag along the strip - the bar under the finger is picked from its
// x position, since a single bar is only a few pixels wide on a phone. Keyboard: the
// strip is one tab stop; Left/Right step a day, Home/End jump to the ends, Escape
// clears. The chosen day is written into the readout line below the strip, which is an
// aria-live region, so a screen reader hears it too.
//
// Listeners sit on the document because public_refresh.js replaces the cards in place.
(function () {
  function days(strip) { return strip.querySelectorAll('.uptime-strip__day'); }

  function select(strip, index) {
    var list = days(strip);
    if (!list.length) return;
    index = Math.max(0, Math.min(list.length - 1, index));
    for (var i = 0; i < list.length; i++) list[i].classList.toggle('selected', i === index);
    var readout = strip.parentNode.querySelector('.uptime__readout');
    if (readout) readout.textContent = list[index].getAttribute('data-label');
    strip.setAttribute('data-index', index);
  }

  function clear(strip) {
    var list = days(strip);
    for (var i = 0; i < list.length; i++) list[i].classList.remove('selected');
    var readout = strip.parentNode.querySelector('.uptime__readout');
    if (readout) readout.textContent = '';
    strip.removeAttribute('data-index');
  }

  function fromPointer(e) {
    var strip = e.target.closest && e.target.closest('.uptime-strip');
    if (!strip) return null;
    var box = strip.getBoundingClientRect();
    var n = days(strip).length;
    if (!box.width || !n) return null;
    return { strip: strip, index: Math.floor((e.clientX - box.left) / box.width * n) };
  }

  var dragging = null;
  document.addEventListener('pointerdown', function (e) {
    var hit = fromPointer(e);
    if (!hit) return;
    dragging = hit.strip;
    select(hit.strip, hit.index);
  });
  document.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    var box = dragging.getBoundingClientRect();
    var n = days(dragging).length;
    select(dragging, Math.floor((e.clientX - box.left) / box.width * n));
  });
  ['pointerup', 'pointercancel'].forEach(function (name) {
    document.addEventListener(name, function () { dragging = null; });
  });

  document.addEventListener('keydown', function (e) {
    var strip = e.target.closest && e.target.closest('.uptime-strip');
    if (!strip) return;
    var n = days(strip).length;
    var current = parseInt(strip.getAttribute('data-index'), 10);
    var next;
    if (e.key === 'ArrowLeft') next = isNaN(current) ? n - 1 : current - 1;
    else if (e.key === 'ArrowRight') next = isNaN(current) ? n - 1 : current + 1;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    else if (e.key === 'Escape') { clear(strip); return; }
    else return;
    e.preventDefault();
    select(strip, next);
  });
  document.addEventListener('focusout', function (e) {
    var strip = e.target.closest && e.target.closest('.uptime-strip');
    if (strip && !dragging) clear(strip);
  });
})();
