// Живое обновление страницы мониторинга через SSE. Только textContent: данные с Metacritic
// (названия игр в ленте) в HTML не вставляем.
(function () {
  "use strict";
  var root = document.getElementById("monitor");
  if (!root || !window.EventSource) return;

  var live = root.querySelector("[data-live]");
  var liveText = root.querySelector("[data-live-text]");
  var statusEl = root.querySelector("[data-status]");
  var eventsEl = root.querySelector("[data-events]");
  var runsKey = root.dataset.runsKey || "";

  function setText(name, value) {
    root.querySelectorAll('[data-field="' + name + '"]').forEach(function (el) {
      if (el.textContent !== String(value)) el.textContent = value;
    });
  }

  function setLive(ok) {
    live.classList.toggle("live--on", ok);
    live.classList.toggle("live--off", !ok);
    liveText.textContent = ok ? "в эфире" : "нет связи, переподключаемся…";
  }

  function renderEvents(events) {
    var items = events.map(function (e) {
      var li = document.createElement("li");
      li.className = "events__item events__item--" + e.level.replace(/[^a-z]/g, "");
      var t = document.createElement("time");
      t.className = "events__time";
      t.textContent = e.time;
      var m = document.createElement("span");
      m.className = "events__msg";
      m.textContent = e.message;
      li.append(t, m);
      return li;
    });
    if (!items.length) {
      var empty = document.createElement("li");
      empty.className = "events__empty";
      empty.textContent = "Событий пока нет.";
      items.push(empty);
    }
    eventsEl.replaceChildren.apply(eventsEl, items);
  }

  function update(s) {
    [
      "status_label", "stage", "current_game", "elapsed", "next_run_in", "next_run_at", "auto_label",
      "processed", "batch_size", "total_games", "processed_today", "errors_today",
    ].forEach(function (k) { setText(k, s[k] == null ? "—" : s[k]); });
    setText("llm_spent", s.llm_spent.toFixed(4));
    setText("llm_tokens", s.llm_input_tokens + " / " + s.llm_output_tokens);
    statusEl.className = "state state--" + s.status;

    var bar = root.querySelector('[data-bar="progress"]');
    bar.max = s.batch_size;
    bar.value = s.processed;
    root.querySelector('[data-bar="llm"]').value = s.llm_percent;

    setText("next_run_note", s.next_run_note || "");
    renderEvents(s.events);

    // Режим выключился по сроку или в другой вкладке — перерисовать кнопку.
    var autoBox = document.getElementById("auto-box");
    if (autoBox && window.htmx && autoBox.dataset.enabled !== String(s.auto_enabled)) {
      autoBox.dataset.enabled = String(s.auto_enabled);
      htmx.ajax("GET", "/monitor/auto", { target: "#auto-box", swap: "outerHTML" });
    }

    if (s.runs_key !== runsKey) {
      runsKey = s.runs_key;
      if (window.htmx) htmx.ajax("GET", root.dataset.runs, { target: "#runs", swap: "innerHTML" });
    }
  }

  var source = new EventSource(root.dataset.stream);
  source.addEventListener("open", function () { setLive(true); });
  source.addEventListener("error", function () { setLive(false); });
  source.addEventListener("state", function (ev) {
    setLive(true);
    try { update(JSON.parse(ev.data)); } catch (err) { console.error(err); }
  });
})();
