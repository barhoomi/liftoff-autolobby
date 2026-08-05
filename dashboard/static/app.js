/* Dashboard frontend. Vanilla JS, no build step (decision D4).
 *
 * The token lives in localStorage and travels as an X-Auth-Token header on fetches; the
 * SSE stream gets it as a query parameter because EventSource cannot set headers. That
 * is acceptable only because of decision D1 (bound to localhost/LAN, reached over a
 * tunnel) -- if this ever becomes internet-facing, that URL is the first thing to fix.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "fpv_dashboard_token";
  var token = localStorage.getItem(TOKEN_KEY) || "";
  var stream = null;
  var streamRows = [];
  var filterText = "";
  var mutedKinds = new Set();
  var selectedLog = null;
  var stateTimer = null;

  var $ = function (id) { return document.getElementById(id); };

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({ "X-Auth-Token": token }, options.headers || {});
    if (options.body !== undefined && typeof options.body !== "string") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    return fetch(path, options).then(function (res) {
      if (res.status === 401) { lock(); throw new Error("unauthorized"); }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          // FastAPI's `detail` is a string for simple errors and an object
          // ({message, findings}) for validation refusals; JSON-encode the latter so the
          // caller can re-parse it instead of getting "[object Object]".
          var detail = data.detail === undefined ? res.statusText : data.detail;
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        return data;
      });
    });
  }
  window.api = api;

  function toast(message, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }
  window.toast = toast;

  /* ---------- auth gate ---------- */

  function unlock() {
    $("gate").hidden = true;
    $("app").hidden = false;
    startStream();
    refreshState();
    refreshLogs();
    if (window.dashboardWriteInit) window.dashboardWriteInit();
    stateTimer = setInterval(refreshState, 5000);
  }

  function lock() {
    localStorage.removeItem(TOKEN_KEY);
    token = "";
    if (stream) { stream.close(); stream = null; }
    if (stateTimer) { clearInterval(stateTimer); stateTimer = null; }
    $("app").hidden = true;
    $("gate").hidden = false;
  }

  $("gate-form").addEventListener("submit", function (e) {
    e.preventDefault();
    token = $("gate-token").value.trim();
    api("/api/auth/check").then(function () {
      localStorage.setItem(TOKEN_KEY, token);
      $("gate-error").hidden = true;
      unlock();
    }).catch(function () {
      $("gate-error").textContent = "Rejected.";
      $("gate-error").hidden = false;
    });
  });

  $("lock").addEventListener("click", lock);

  /* ---------- tabs ---------- */

  document.getElementById("tabs").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    Array.prototype.forEach.call(document.querySelectorAll("#tabs button"), function (b) {
      b.classList.toggle("active", b === btn);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (section) {
      section.classList.toggle("active", section.id === "tab-" + btn.dataset.tab);
    });
  });

  /* ---------- helpers ---------- */

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    seconds = Math.floor(seconds);
    var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + String(s).padStart(2, "0") + "s";
    return s + "s";
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
    var units = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + units[i];
  }

  function localTime(ts) {
    if (!ts) return "";
    var d = new Date(ts);
    return isNaN(d) ? ts : d.toLocaleTimeString();
  }

  function stat(key, value, sub) {
    var el = document.createElement("div");
    el.className = "stat";
    var k = document.createElement("div"); k.className = "k"; k.textContent = key;
    var v = document.createElement("div"); v.className = "v"; v.textContent = value;
    el.appendChild(k); el.appendChild(v);
    if (sub) {
      var s = document.createElement("div"); s.className = "sub"; s.textContent = sub;
      el.appendChild(s);
    }
    return el;
  }

  /* ---------- state panel ---------- */

  function refreshState() {
    return api("/api/state").then(function (snapshot) {
      window.dashboardState = snapshot;
      renderState(snapshot);
      if (window.dashboardWriteRender) window.dashboardWriteRender(snapshot);
    }).catch(function (err) {
      if (err.message !== "unauthorized") toast(err.message, "err");
    });
  }
  window.refreshState = refreshState;

  function renderState(s) {
    var cfg = s.config, rot = s.rotation, live = s.live;
    var cards = $("state-cards");
    cards.textContent = "";
    cards.appendChild(stat("Track", rot.current_track || "—",
      [rot.current_environment, rot.current_mode].filter(Boolean).join(" · ")));
    cards.appendChild(stat("Next rotation", fmtDuration(rot.remaining_s),
      rot.paused ? "paused" : (!rot.engaged ? "disengaged" : "of " + fmtDuration(rot.interval_s))));
    cards.appendChild(stat("Playlist", cfg.playlist || "—", cfg.track_count + " tracks"));
    cards.appendChild(stat("Lobby", cfg.lobby_name || "—",
      (cfg.room_private === false ? "public" : "private") +
      (cfg.max_players ? " · max " + cfg.max_players : "")));
    cards.appendChild(stat("Uptime", fmtDuration(live.uptime_s),
      live.game_pid ? "game pid " + live.game_pid : ""));
    cards.appendChild(stat("Modes",
      [cfg.auto_start ? "auto-start" : null, cfg.shuffle_mode ? "shuffle" : null,
       cfg.democracy_mode ? "democracy" : null, cfg.maintenance_active ? "MAINTENANCE" : null]
      .filter(Boolean).join(", ") || "none", cfg.override_game_mode || ""));

    var players = $("players");
    players.textContent = "";
    $("player-count").textContent = live.player_count === null || live.player_count === undefined
      ? String(live.players.length) : live.player_count + " in room";
    if (!live.players.length) {
      var empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "nobody tracked (no join events since the last restart)";
      players.appendChild(empty);
    }
    live.players.forEach(function (p) {
      var li = document.createElement("li");
      li.textContent = (p.player || "(unknown)") + (p.userId ? "  " + p.userId : "");
      players.appendChild(li);
    });

    var list = $("rotation-list");
    list.textContent = "";
    cfg.tracks.slice(0, 200).forEach(function (t, i) {
      var li = document.createElement("li");
      li.textContent = t.track + " — " + t.environment + (t.mode ? " (" + t.mode + ")" : "");
      if (t.track === rot.current_track) li.className = "current";
      list.appendChild(li);
    });

    var errors = live.recent_errors || [];
    $("errors-card").hidden = errors.length === 0;
    var ul = $("errors");
    ul.textContent = "";
    errors.slice().reverse().forEach(function (e) {
      var li = document.createElement("li");
      li.textContent = localTime(e.ts) + "  [" + (e.context || e.source || "?") + "] " + e.message;
      ul.appendChild(li);
    });
  }

  /* ---------- live event stream ---------- */

  var KINDS = ["chat", "rotation", "player_join", "player_leave", "decision",
               "disconnect", "error", "scene_change", "admin_command_result"];

  function buildKindFilters() {
    var box = $("kind-filters");
    box.textContent = "";
    KINDS.forEach(function (kind) {
      var label = document.createElement("label");
      label.className = "check";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.checked = true;
      input.addEventListener("change", function () {
        if (input.checked) mutedKinds.delete(kind); else mutedKinds.add(kind);
        applyFilters();
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(kind));
      box.appendChild(label);
    });
  }

  function describe(ev) {
    switch (ev.event) {
      case "chat": return (ev.player || "?") + ": " + (ev.msg || "");
      case "rotation": return (ev.track || "") + " — " + (ev.env || "") +
        (ev.mode ? " (" + ev.mode + ")" : "") + (ev.index !== undefined ? "  #" + ev.index : "");
      case "player_join":
      case "player_leave": return (ev.player || ev.userId || "?") +
        (ev.count !== undefined ? "  (" + ev.count + " in room)" : "");
      case "disconnect": return (ev.cause || "?") +
        (ev.elapsed_s !== undefined ? " after " + ev.elapsed_s + "s" : "");
      case "decision": return (ev.kind || "") + ": " + (ev.detail || "");
      case "error": return (ev.context ? "[" + ev.context + "] " : "") + (ev.message || "");
      case "scene_change": return (ev.from || "") + " -> " + (ev.to || ev.scene || "");
      case "admin_command_result": return (ev.cmd || "") + " by " + (ev.user_name || "?") +
        " -> " + (ev.result || "");
      case "playlist_resolved": return (ev.playlist || "") + " · " + ev.track_count + " tracks";
      case "_unparseable": return ev.raw || "";
      default:
        return Object.keys(ev).filter(function (k) {
          return ["ts", "source", "event", "_file"].indexOf(k) === -1;
        }).map(function (k) { return k + "=" + ev[k]; }).join(" ");
    }
  }

  function rowMatches(row) {
    if (mutedKinds.has(row.event)) return false;
    if (!filterText) return true;
    return row.text.toLowerCase().indexOf(filterText) !== -1;
  }

  function applyFilters() {
    streamRows.forEach(function (row) { row.el.hidden = !rowMatches(row); });
  }

  function appendEvent(ev) {
    var box = $("stream");
    var el = document.createElement("div");
    el.className = "ev";
    el.dataset.source = ev.source || "";
    el.dataset.event = ev.event || "";
    var ts = document.createElement("span"); ts.className = "ts"; ts.textContent = localTime(ev.ts);
    var name = document.createElement("span"); name.className = "name"; name.textContent = ev.event || "?";
    var body = document.createElement("span"); body.className = "body"; body.textContent = describe(ev);
    el.appendChild(ts); el.appendChild(name); el.appendChild(body);

    var row = { el: el, event: ev.event, text: (ev.event || "") + " " + body.textContent };
    row.el.hidden = !rowMatches(row);
    streamRows.push(row);
    box.appendChild(el);

    while (streamRows.length > 1000) {
      var dropped = streamRows.shift();
      dropped.el.remove();
    }
    if ($("autoscroll").checked) box.scrollTop = box.scrollHeight;
  }

  function startStream() {
    if (stream) stream.close();
    stream = new EventSource("/api/events/stream?backlog=100&token=" + encodeURIComponent(token));
    stream.onopen = function () { $("conn-dot").className = "dot live"; };
    stream.onerror = function () { $("conn-dot").className = "dot down"; };
    stream.onmessage = function (msg) {
      $("conn-dot").className = "dot live";
      try { appendEvent(JSON.parse(msg.data)); } catch (e) { /* ignore a torn frame */ }
    };
  }

  $("filter").addEventListener("input", function (e) {
    filterText = e.target.value.trim().toLowerCase();
    applyFilters();
  });
  $("clear-stream").addEventListener("click", function () {
    streamRows.forEach(function (row) { row.el.remove(); });
    streamRows = [];
  });

  /* ---------- logs ---------- */

  function refreshLogs() {
    return api("/api/logs").then(function (data) {
      var ul = $("log-list");
      ul.textContent = "";
      data.logs.forEach(function (entry) {
        var li = document.createElement("li");
        var btn = document.createElement("button");
        btn.className = "ghost";
        btn.textContent = entry.label;
        btn.disabled = !entry.exists || !entry.readable;
        btn.addEventListener("click", function () { openLog(entry); });
        li.appendChild(btn);
        var meta = document.createElement("span");
        meta.className = "muted";
        meta.textContent = "  " + (entry.note ? entry.note : fmtBytes(entry.size));
        li.appendChild(meta);
        ul.appendChild(li);
      });
    }).catch(function (err) {
      if (err.message !== "unauthorized") toast(err.message, "err");
    });
  }

  function openLog(entry) {
    selectedLog = entry;
    $("log-title").textContent = entry.label;
    loadLog();
  }

  function loadLog() {
    if (!selectedLog) return;
    var lines = $("log-lines").value;
    api("/api/logs/" + encodeURIComponent(selectedLog.id) + "/tail?lines=" + lines)
      .then(function (data) {
        $("log-body").textContent = data.text || "(empty)";
        $("log-body").scrollTop = $("log-body").scrollHeight;
      })
      .catch(function (err) { $("log-body").textContent = "Error: " + err.message; });
  }

  $("log-refresh").addEventListener("click", function () { refreshLogs(); loadLog(); });
  $("log-lines").addEventListener("change", loadLog);

  /* ---------- boot ---------- */

  buildKindFilters();
  if (token) {
    api("/api/auth/check").then(unlock).catch(function () { lock(); });
  }
})();
