/* Write-side UI: playlist manager + bot controls.
 *
 * Hooks into app.js through window.api / window.toast / window.refreshState and the two
 * lifecycle hooks it calls (dashboardWriteInit after unlock, dashboardWriteRender on each
 * state poll), so the read side stays usable on its own.
 *
 * Playlists are edited as text rather than through a form builder: an entry is
 * `environment | track pattern | mode`, which is compact enough to type on a phone and
 * maps 1:1 onto the JSON the API takes. Validation is the server's job (it runs the same
 * trackcheck lint the pre-commit check runs), so this file never second-guesses a name.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var editing = null;
  var lastPlaylists = {};

  function parseEntries(text) {
    return text.split("\n").map(function (line) { return line.trim(); })
      .filter(Boolean)
      .map(function (line) {
        var parts = line.split("|").map(function (p) { return p.trim(); });
        return {
          environment: parts[0] || "*",
          track: parts.length > 1 ? (parts[1] || "*") : "*",
          mode: parts.length > 2 && parts[2] ? parts[2] : "Infinite Race"
        };
      });
  }

  function formatEntries(items) {
    return (items || []).map(function (item) {
      if (typeof item === "string") return "* | " + item + " | Infinite Race";
      return [item.environment || "*", item.track || "*", item.mode || "Infinite Race"].join(" | ");
    }).join("\n");
  }

  function showFindings(findings) {
    var ul = $("playlist-findings");
    ul.textContent = "";
    (findings || []).forEach(function (f) {
      var li = document.createElement("li");
      li.className = "finding " + (f.severity === "blocking" ? "blocking" : "");
      li.textContent = f.code + ": " + f.detail;
      ul.appendChild(li);
    });
    if (!findings || !findings.length) {
      var ok = document.createElement("li");
      ok.className = "muted";
      ok.textContent = "no findings";
      ul.appendChild(ok);
    }
  }

  function errorDetail(err) {
    // The API returns either a plain string detail or {message, findings}.
    try {
      var parsed = JSON.parse(err.message);
      if (parsed && parsed.message) { showFindings(parsed.findings); return parsed.message; }
    } catch (e) { /* not JSON */ }
    return err.message;
  }

  /* ---------- playlist manager ---------- */

  function refreshPlaylists() {
    return window.api("/api/playlists").then(function (data) {
      lastPlaylists = data.playlists;
      $("master-warning").hidden = data.master_tracks_available;
      var ul = $("playlist-list");
      ul.textContent = "";
      Object.keys(data.playlists).sort().forEach(function (name) {
        var findings = data.findings[name] || [];
        var li = document.createElement("li");
        li.className = "playlist-row";

        var label = document.createElement("span");
        label.className = "name" + (name === data.active ? " active" : "");
        label.textContent = name + " (" + data.playlists[name].length + " entries)";
        li.appendChild(label);

        if (findings.length) {
          var pill = document.createElement("span");
          pill.className = "pill";
          pill.textContent = findings.length + " issue" + (findings.length > 1 ? "s" : "");
          li.appendChild(pill);
        }
        if (name === data.active) {
          var activePill = document.createElement("span");
          activePill.className = "pill";
          activePill.textContent = "active";
          li.appendChild(activePill);
        }

        var edit = document.createElement("button");
        edit.className = "ghost";
        edit.textContent = "edit";
        edit.addEventListener("click", function () { openEditor(name, data.playlists[name], findings); });
        li.appendChild(edit);

        var activate = document.createElement("button");
        activate.className = "ghost";
        activate.textContent = "activate";
        activate.disabled = name === data.active;
        activate.addEventListener("click", function () { activate_(name); });
        li.appendChild(activate);

        ul.appendChild(li);
      });
    }).catch(function (err) { window.toast(errorDetail(err), "err"); });
  }

  function openEditor(name, items, findings) {
    editing = name;
    $("playlist-editor").hidden = false;
    $("editing-name").textContent = name;
    $("playlist-body").value = formatEntries(items);
    showFindings(findings);
    $("playlist-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function save(force) {
    if (!editing) return Promise.resolve();
    var items = parseEntries($("playlist-body").value);
    return window.api("/api/playlists/" + encodeURIComponent(editing), {
      method: "PUT", body: { items: items, force: !!force }
    }).then(function (data) {
      showFindings(data.findings);
      window.toast("Saved " + editing, "ok");
      return refreshPlaylists();
    }).catch(function (err) {
      var message = errorDetail(err);
      if (!force && /force=true/.test(message)) {
        if (confirm(message + "\n\nSave anyway?")) return save(true);
        return;
      }
      window.toast(message, "err");
      throw err;
    });
  }

  function activate_(name) {
    return window.api("/api/playlists/" + encodeURIComponent(name) + "/activate", {
      method: "POST", body: {}
    }).then(function (data) {
      window.toast("Activated " + name +
        (data.resolved_tracks !== null ? " (" + data.resolved_tracks + " tracks)" : ""), "ok");
      refreshPlaylists();
      window.refreshState();
    }).catch(function (err) { window.toast(errorDetail(err), "err"); });
  }

  $("playlist-validate").addEventListener("click", function () {
    window.api("/api/playlists/" + encodeURIComponent(editing) + "/validate", {
      method: "POST", body: { items: parseEntries($("playlist-body").value) }
    }).then(function (data) {
      showFindings(data.findings);
      window.toast(data.blocking + " blocking, " + data.warnings + " warning(s)",
                   data.blocking ? "err" : "ok");
    }).catch(function (err) { window.toast(errorDetail(err), "err"); });
  });

  $("playlist-save").addEventListener("click", function () { save(false); });

  $("playlist-activate").addEventListener("click", function () {
    var name = editing;
    save(false).then(function () { if (name) activate_(name); });
  });

  $("playlist-delete").addEventListener("click", function () {
    if (!editing || !confirm("Delete playlist '" + editing + "'?")) return;
    window.api("/api/playlists/" + encodeURIComponent(editing), { method: "DELETE" })
      .then(function () {
        window.toast("Deleted " + editing, "ok");
        editing = null;
        $("playlist-editor").hidden = true;
        refreshPlaylists();
      })
      .catch(function (err) { window.toast(errorDetail(err), "err"); });
  });

  $("playlist-cancel").addEventListener("click", function () {
    editing = null;
    $("playlist-editor").hidden = true;
  });

  $("new-playlist").addEventListener("click", function () {
    var name = $("new-playlist-name").value.trim();
    if (!name) return;
    if (lastPlaylists[name]) { window.toast("That name already exists", "err"); return; }
    $("new-playlist-name").value = "";
    openEditor(name, [{ environment: "*", track: "*", mode: "Infinite Race" }], []);
  });

  /* ---------- controls ---------- */

  function post(path, body, message) {
    return window.api(path, { method: "POST", body: body || {} })
      .then(function (data) {
        window.toast(message || "Applied", "ok");
        window.refreshState();
        return data;
      })
      .catch(function (err) { window.toast(errorDetail(err), "err"); throw err; });
  }

  $("ctl-interval-save").addEventListener("click", function () {
    post("/api/control/interval", { seconds: parseInt($("ctl-interval").value, 10) },
         "Rotation interval set");
  });
  $("ctl-paused").addEventListener("change", function (e) {
    post("/api/control/rotation", { paused: e.target.checked },
         e.target.checked ? "Rotation paused" : "Rotation resumed");
  });
  $("ctl-engaged").addEventListener("change", function (e) {
    post("/api/control/rotation", { engaged: e.target.checked },
         e.target.checked ? "Rotation engaged" : "Rotation disengaged");
  });
  $("ctl-shuffle").addEventListener("change", function (e) {
    post("/api/control/shuffle", { enabled: e.target.checked }, "Shuffle updated");
  });
  $("ctl-autostart").addEventListener("change", function (e) {
    post("/api/control/auto-start", { enabled: e.target.checked }, "Auto-start updated");
  });
  $("ctl-democracy").addEventListener("change", function (e) {
    post("/api/control/democracy", { enabled: e.target.checked }, "Democracy mode updated");
  });
  $("ctl-lobby-save").addEventListener("click", function () {
    var body = { name: $("ctl-lobby-name").value, private: !$("ctl-public").checked };
    var maxPlayers = parseInt($("ctl-max-players").value, 10);
    if (!isNaN(maxPlayers)) body.max_players = maxPlayers;
    post("/api/control/lobby", body, "Lobby settings queued for the next room update");
  });
  $("ctl-game-mode-save").addEventListener("click", function () {
    post("/api/control/game-mode", { mode: $("ctl-game-mode").value || null }, "Game mode updated");
  });
  $("ctl-maintenance").addEventListener("click", function () {
    if (!confirm("Schedule a maintenance shutdown? The bot announces it in chat and quits.")) return;
    post("/api/control/maintenance", { enabled: true }, "Maintenance scheduled");
  });
  $("ctl-maintenance-cancel").addEventListener("click", function () {
    post("/api/control/maintenance", { enabled: false }, "Maintenance cancelled");
  });
  $("ctl-skip").addEventListener("click", function () {
    window.api("/api/control/skip", { method: "POST" })
      .catch(function (err) { window.toast(errorDetail(err), "err"); });
  });
  $("ctl-restart").addEventListener("click", function () {
    if (!confirm("Restart the bot container? The lobby drops until it comes back.")) return;
    post("/api/control/restart", {}, "Restart requested");
  });

  /* ---------- lifecycle hooks called by app.js ---------- */

  window.dashboardWriteInit = function () {
    refreshPlaylists();
    window.api("/api/control/info").then(function (info) {
      $("ctl-skip").disabled = !info.skip_supported;
      $("ctl-skip-note").textContent = info.skip_supported ? "" : "needs a plugin change";
      var available = info.settings.restart_available;
      $("ctl-restart").disabled = !available;
      $("ctl-restart-note").textContent = available
        ? info.settings.restart_command.join(" ")
        : "disabled in config";
    }).catch(function () { /* the state panel already reports auth/API problems */ });
  };

  // Keep the control inputs showing what the bot is actually configured with, without
  // stomping on a field the operator is currently typing in.
  window.dashboardWriteRender = function (snapshot) {
    var cfg = snapshot.config;
    function setIfIdle(el, value) {
      if (document.activeElement !== el && value !== null && value !== undefined) el.value = value;
    }
    setIfIdle($("ctl-interval"), cfg.rotation_interval_s);
    setIfIdle($("ctl-lobby-name"), cfg.lobby_name);
    setIfIdle($("ctl-max-players"), cfg.max_players);
    setIfIdle($("ctl-game-mode"), cfg.override_game_mode || "");
    $("ctl-public").checked = cfg.room_private === false;
    $("ctl-paused").checked = !!cfg.rotation_paused;
    $("ctl-engaged").checked = cfg.rotation_engaged !== false;
    $("ctl-shuffle").checked = !!cfg.shuffle_mode;
    $("ctl-autostart").checked = !!cfg.auto_start;
    $("ctl-democracy").checked = !!cfg.democracy_mode;
    $("ctl-maintenance-cancel").hidden = !cfg.maintenance_active;
  };
})();
