(function () {
  var boxes = Array.prototype.slice.call(document.querySelectorAll('.pin-box'));
  var form = document.getElementById('pin-form');
  var hidden = document.getElementById('pin-value');
  var errorEl = document.getElementById('login-error');

  function clearError() { errorEl.textContent = ''; }

  function submitIfComplete() {
    var pin = boxes.map(function (b) { return b.value; }).join('');
    if (/^\d{4}$/.test(pin)) {
      hidden.value = pin;
      form.submit();
    }
  }

  function fill(startIndex, digits) {
    for (var i = 0; i < digits.length && startIndex + i < boxes.length; i++) {
      boxes[startIndex + i].value = digits[i];
    }
    var next = Math.min(startIndex + digits.length, boxes.length - 1);
    boxes[next].focus();
    submitIfComplete();
  }

  boxes.forEach(function (box, i) {
    box.addEventListener('input', function () {
      clearError();
      var digits = box.value.replace(/\D/g, '');
      box.value = '';
      if (digits) fill(i, digits);
    });
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Backspace' && !box.value && i > 0) {
        boxes[i - 1].value = '';
        boxes[i - 1].focus();
      } else if (e.key === 'ArrowLeft' && i > 0) {
        boxes[i - 1].focus();
      } else if (e.key === 'ArrowRight' && i < boxes.length - 1) {
        boxes[i + 1].focus();
      }
    });
    box.addEventListener('paste', function (e) {
      e.preventDefault();
      var text = (e.clipboardData || window.clipboardData).getData('text').replace(/\D/g, '');
      if (text) fill(i, text.slice(0, boxes.length - i));
    });
  });

  // After a failed attempt the server re-renders the page; start clean.
  boxes[0].focus();
})();
