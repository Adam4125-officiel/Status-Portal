(function () {
  function utcValue(offsetMinutes) {
    // datetime-local wants "YYYY-MM-DDTHH:MM" with no timezone. toISOString() is always
    // UTC, so slicing off seconds/ms/Z gives exactly that - in UTC, which is what the
    // server compares against. Same helper as admin_maintenance_form.js.
    var d = new Date(Date.now() + (offsetMinutes || 0) * 60000);
    return d.toISOString().slice(0, 16);
  }

  var starts = document.getElementById('starts_at');
  var ends = document.getElementById('ends_at');
  if (!starts || !ends) return;

  var WEEK = 60 * 24 * 7;

  // Filled on first focus rather than on page load, and this is the one difference from
  // the maintenance form that is deliberate. A maintenance window must have both ends,
  // so pre-filling it is pure convenience. An announcement's window is optional, and
  // blank is the common case - "show it until I delete it", which is what every
  // announcement did before windows existed. Pre-filling on load would silently give
  // every new announcement an expiry nobody asked for, and the admin would only find
  // out when it vanished. Filling on focus gives the same "don't make me type a UTC
  // timestamp" convenience at the moment the admin has actually reached for the field.
  function fillOnFirstFocus(input, offsetMinutes) {
    input.addEventListener('focus', function () {
      if (!input.value) input.value = utcValue(offsetMinutes);
    });
  }
  fillOnFirstFocus(starts, 0);
  fillOnFirstFocus(ends, WEEK);

  function wire(id, input, offsetMinutes) {
    var btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      input.value = utcValue(offsetMinutes);
    });
  }
  wire('starts_at_now', starts, 0);
  // A week, not the maintenance form's hour: an announcement that expired an hour after
  // being written would almost never be what was wanted.
  wire('ends_at_now', ends, WEEK);

  var clear = document.getElementById('window_clear');
  if (clear) {
    clear.addEventListener('click', function (e) {
      e.preventDefault();
      starts.value = '';
      ends.value = '';
      // Focus would immediately refill it via the handler above, so don't.
      clear.blur();
    });
  }
})();
