(function () {
  // Display-only copy of BUDGET_RECOMMEND_DISCLAIMER (app/prompts/budget_system_prompt.py).
  // The backend guarantees it appears exactly once; this just lets us render it
  // separately in muted, smaller text.
  var DISCLAIMER = 'This is general budgeting guidance, not financial advice.';

  var history = document.getElementById('history');
  var composer = document.getElementById('composer');
  var input = document.getElementById('question');
  var sendBtn = document.getElementById('send-btn');
  var uploadBtn = document.getElementById('upload-btn');
  var fileInput = document.getElementById('file-input');
  var uploadStatus = document.getElementById('upload-status');
  var dataStatus = document.getElementById('data-status');
  var personaSelect = document.getElementById('persona-select');
  var personaHint = document.getElementById('persona-hint');
  var busy = false;

  // Everything below builds DOM with textContent, never innerHTML, so model
  // output and error text can't inject markup.
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Appends `text` to `node`, turning **double-asterisk** spans into <strong>
  // elements. Built from text nodes, so it stays markup-safe.
  function appendRich(node, text) {
    String(text).split(/\*\*(.+?)\*\*/g).forEach(function (part, i) {
      if (!part) return;
      node.appendChild(i % 2 ? el('strong', null, part) : document.createTextNode(part));
    });
    return node;
  }

  function confidenceLabel(conf) {
    if (conf >= 0.8) return 'High';
    if (conf >= 0.5) return 'Medium';
    return 'Low';
  }

  function renderBreakdown(table) {
    var wrap = el('div', 'breakdown');
    if (table.title) wrap.appendChild(el('div', 'breakdown-title', table.title));
    var scroller = el('div', 'table-wrap');
    var t = el('table');
    var head = el('thead');
    var headRow = el('tr');
    (table.columns || []).forEach(function (c) { headRow.appendChild(el('th', null, String(c))); });
    head.appendChild(headRow);
    t.appendChild(head);
    function addRows(section, rows) {
      (rows || []).forEach(function (r) {
        var tr = el('tr');
        r.forEach(function (cell) { tr.appendChild(el('td', null, String(cell))); });
        section.appendChild(tr);
      });
      t.appendChild(section);
    }
    addRows(el('tbody'), table.rows);
    if (table.footer && table.footer.length) addRows(el('tfoot'), table.footer);
    scroller.appendChild(t);
    wrap.appendChild(scroller);
    if (table.note) wrap.appendChild(el('div', 'breakdown-note', table.note));
    return wrap;
  }

  function scrollDown() { history.scrollTop = history.scrollHeight; }

  function addUser(text) {
    var turn = el('div', 'turn turn-user');
    turn.appendChild(el('div', 'bubble bubble-user', text));
    history.appendChild(turn);
    scrollDown();
  }

  function addPending() {
    var turn = el('div', 'turn turn-assistant');
    turn.appendChild(el('div', 'bubble bubble-assistant pending', 'Reading your transaction history…'));
    history.appendChild(turn);
    scrollDown();
    return turn;
  }

  function splitDisclaimer(answer, explanation) {
    var found = false;
    if (answer.indexOf(DISCLAIMER) !== -1) { answer = answer.split(DISCLAIMER).join('').trim(); found = true; }
    if (explanation.indexOf(DISCLAIMER) !== -1) { explanation = explanation.split(DISCLAIMER).join('').trim(); found = true; }
    return { answer: answer, explanation: explanation, disclaimer: found ? DISCLAIMER : null };
  }

  function renderAnswer(turn, data) {
    var parts = splitDisclaimer(String(data.answer || ''), String(data.explanation || ''));
    var bubble = el('div', 'bubble bubble-assistant');

    var chips = el('div', 'chip-row');
    chips.appendChild(el('span', 'chip chip-type', String(data.question_type || 'general').replace(/_/g, ' ')));
    if (data.cache_hit) chips.appendChild(el('span', 'chip chip-cache', 'cached'));
    bubble.appendChild(chips);

    bubble.appendChild(appendRich(el('div', 'answer'), parts.answer));
    if (data.period) bubble.appendChild(el('div', 'period', 'Period: ' + String(data.period)));
    if (data.breakdown && Array.isArray(data.breakdown.rows)) bubble.appendChild(renderBreakdown(data.breakdown));
    if (parts.explanation) bubble.appendChild(appendRich(el('div', 'explanation'), parts.explanation));

    // Sources are deliberately not shown: every answer comes from the user's
    // own transactions. Confidence is, as a plain-language level.
    var conf = Math.max(0, Math.min(1, Number(data.confidence) || 0));
    var row = el('div', 'confidence-row');
    row.appendChild(el('span', 'confidence-label', 'Confidence'));
    var track = el('div', 'confidence-track');
    var fill = el('div', 'confidence-fill');
    fill.style.width = Math.round(conf * 100) + '%';
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('span', 'confidence-value', confidenceLabel(conf)));
    bubble.appendChild(row);

    if (parts.disclaimer) bubble.appendChild(el('div', 'disclaimer', parts.disclaimer));

    turn.replaceChildren(bubble);
    scrollDown();
  }

  function renderError(turn, data) {
    var bubble = el('div', 'bubble bubble-error');
    var chips = el('div', 'chip-row');
    chips.appendChild(el('span', 'chip chip-error', String((data && data.error) || 'ERR_UNKNOWN')));
    bubble.appendChild(chips);
    bubble.appendChild(el('div', 'answer', String((data && data.message) || 'Something went wrong.')));
    turn.replaceChildren(bubble);
    scrollDown();
  }

  function setBusy(value) {
    busy = value;
    sendBtn.disabled = value;
    input.disabled = value;
    if (!value) input.focus();
  }

  // The free AI tier allows roughly one question a minute. After a rate
  // limit, hold the Send button with a countdown instead of letting people
  // retry straight into the same limit.
  var COOLDOWN_SECONDS = 60;
  function startCooldown() {
    var remaining = COOLDOWN_SECONDS;
    var label = sendBtn.textContent;
    busy = true;
    sendBtn.disabled = true;
    var timer = setInterval(function () {
      remaining -= 1;
      sendBtn.textContent = 'Wait ' + remaining + 's';
      if (remaining <= 0) {
        clearInterval(timer);
        sendBtn.textContent = label;
        setBusy(false);
      }
    }, 1000);
    sendBtn.textContent = 'Wait ' + remaining + 's';
  }

  function ask(question) {
    question = question.trim();
    if (!question || busy) return;
    addUser(question);
    var turn = addPending();
    setBusy(true);

    fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question })
    }).then(function (res) {
      if (res.status === 401) { window.location.href = '/login'; return null; }
      return res.json().catch(function () { return null; }).then(function (data) {
        return { ok: res.ok, data: data };
      });
    }).then(function (result) {
      if (!result) return false;
      if (result.ok && result.data) { renderAnswer(turn, result.data); return false; }
      renderError(turn, result.data || { error: 'ERR_UNKNOWN', message: 'Something went wrong.' });
      return !!(result.data && result.data.error === 'ERR_MODEL_RATE_LIMIT');
    }).catch(function () {
      renderError(turn, { error: 'ERR_CONNECTION', message: "Couldn't reach the server." });
      return false;
    }).then(function (rateLimited) {
      if (rateLimited) startCooldown();
      else setBusy(false);
    });
  }

  composer.addEventListener('submit', function (e) {
    e.preventDefault();  // Enter in the text box submits the form
    var q = input.value;
    input.value = '';
    ask(q);
  });

  document.getElementById('quick-grid').addEventListener('click', function (e) {
    var btn = e.target.closest('.quick-btn');
    if (btn) ask(btn.textContent);
  });

  // --- Test user (persona) ---
  if (personaSelect) {
    var currentPersona = personaSelect.value;
    personaSelect.addEventListener('change', function () {
      var option = personaSelect.options[personaSelect.selectedIndex];
      personaSelect.disabled = true;
      fetch('/persona', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: personaSelect.value })
      }).then(function (res) {
        if (res.status === 401) { window.location.href = '/login'; return null; }
        return res.json().catch(function () { return null; }).then(function (data) {
          return { ok: res.ok, data: data };
        });
      }).then(function (result) {
        if (!result) return;
        if (result.ok && result.data && result.data.ok) {
          currentPersona = personaSelect.value;
          personaHint.textContent = option.getAttribute('data-hint') || '';
          dataStatus.textContent = 'Using demo data';
          setUploadStatus('', false);
          var turn = el('div', 'turn turn-system');
          turn.appendChild(el('div', 'system-note', 'Now testing as ' + result.data.label));
          history.appendChild(turn);
          scrollDown();
        } else {
          personaSelect.value = currentPersona;
        }
      }).catch(function () {
        personaSelect.value = currentPersona;
      }).then(function () { personaSelect.disabled = false; });
    });
  }

  // --- Upload ---
  function setUploadStatus(text, isError) {
    uploadStatus.textContent = text;
    uploadStatus.className = 'upload-status' + (isError ? ' is-error' : text ? ' is-ok' : '');
  }

  uploadBtn.addEventListener('click', function () { fileInput.click(); });

  fileInput.addEventListener('change', function () {
    var file = fileInput.files[0];
    if (!file) return;
    var form = new FormData();
    form.append('file', file);
    setUploadStatus('Uploading ' + file.name + '…', false);
    uploadBtn.disabled = true;

    fetch('/upload', { method: 'POST', body: form }).then(function (res) {
      if (res.status === 401) { window.location.href = '/login'; return null; }
      return res.json().catch(function () { return null; }).then(function (data) {
        return { ok: res.ok, data: data };
      });
    }).then(function (result) {
      if (!result) return;
      if (result.ok && result.data && result.data.ok) {
        setUploadStatus(result.data.message, false);
        dataStatus.textContent = 'Using your uploaded data';
      } else {
        setUploadStatus((result.data && result.data.message) || 'Upload failed.', true);
      }
    }).catch(function () {
      setUploadStatus("Couldn't reach the server.", true);
    }).then(function () {
      uploadBtn.disabled = false;
      fileInput.value = '';  // allow re-selecting the same file
    });
  });

  input.focus();
})();
