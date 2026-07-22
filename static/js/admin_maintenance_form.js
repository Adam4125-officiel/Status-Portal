(function () {
  function utcValue(offsetMinutes) {
    // datetime-local wants "YYYY-MM-DDTHH:MM" with no timezone - toISOString() is
    // always UTC, so slicing off the seconds/ms/Z gives exactly that, in UTC (which is
    // what the server compares against - see the field hint on this form).
    var d = new Date(Date.now() + (offsetMinutes || 0) * 60000);
    return d.toISOString().slice(0, 16);
  }

  var startsInput = document.getElementById('starts_at');
  var endsInput = document.getElementById('ends_at');
  if (!startsInput || !endsInput) return;

  if (!startsInput.value) startsInput.value = utcValue(0);
  if (!endsInput.value) endsInput.value = utcValue(60);

  var startsNowBtn = document.getElementById('starts_at_now');
  if (startsNowBtn) {
    startsNowBtn.addEventListener('click', function (e) {
      e.preventDefault();
      startsInput.value = utcValue(0);
    });
  }

  var endsNowBtn = document.getElementById('ends_at_now');
  if (endsNowBtn) {
    endsNowBtn.addEventListener('click', function (e) {
      e.preventDefault();
      endsInput.value = utcValue(60);
    });
  }
})();
