/* Factory QC UI behaviour. Progressive enhancement only: every page works without this file. */
(function () {
  "use strict";
  var body = document.body, root = document.documentElement;

  // Responsive sidebar (off-canvas on tablets) and compact rail on desktop
  document.querySelectorAll("[data-nav-toggle]").forEach(function (el) {
    el.addEventListener("click", function () { body.classList.toggle("nav-open"); });
  });
  document.querySelectorAll("[data-rail-toggle]").forEach(function (el) {
    el.addEventListener("click", function () {
      var on = root.getAttribute("data-rail") !== "1";
      if (on) root.setAttribute("data-rail", "1"); else root.removeAttribute("data-rail");
      try { localStorage.setItem("fqc-rail", on ? "1" : "0"); } catch (e) {}
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    body.classList.remove("nav-open");
    document.querySelectorAll("details[data-menu][open], details.menu[open]").forEach(function (d) { d.removeAttribute("open"); });
  });

  // Dropdown menus (details/summary): close on outside click
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details[data-menu][open], details.menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });

  // Toasts: auto-dismiss success/info, animate out
  function dismiss(t) { t.classList.add("leaving"); setTimeout(function () { t.remove(); }, 200); }
  document.querySelectorAll(".toast").forEach(function (t) {
    var close = t.querySelector("button");
    if (close) close.addEventListener("click", function () { dismiss(t); });
    if (t.classList.contains("success") || t.classList.contains("info")) setTimeout(function () { dismiss(t); }, 6000);
  });

  // Show / hide password
  document.querySelectorAll("[data-pw-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-pw-toggle"));
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
    });
  });

  // Confirmation dialog for irreversible actions: <form data-confirm="..."> or <button data-confirm="...">
  var dlg = document.getElementById("confirm-dialog");
  if (dlg && typeof dlg.showModal === "function") {
    var pending = null;
    document.addEventListener("submit", function (e) {
      var form = e.target;
      var trigger = e.submitter;
      var msg = (trigger && trigger.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
      if (!msg || form.dataset.confirmed === "1") return;
      e.preventDefault();
      pending = { form: form, submitter: trigger };
      dlg.querySelector("[data-confirm-text]").textContent = msg;
      dlg.showModal();
      var ok = dlg.querySelector("[data-confirm-ok]"); if (ok) ok.focus();
    });
    dlg.querySelector("[data-confirm-cancel]").addEventListener("click", function () { pending = null; dlg.close(); });
    dlg.addEventListener("click", function (e) { if (e.target === dlg) { pending = null; dlg.close(); } });
    dlg.querySelector("[data-confirm-ok]").addEventListener("click", function () {
      dlg.close();
      if (!pending) return;
      pending.form.dataset.confirmed = "1";
      if (pending.form.requestSubmit && pending.submitter) pending.form.requestSubmit(pending.submitter);
      else pending.form.submit();
    });
  }

  // Client-side table search: <input data-table-filter="table-id">; shows a "no matches" row
  document.querySelectorAll("[data-table-filter]").forEach(function (input) {
    var table = document.getElementById(input.getAttribute("data-table-filter"));
    if (!table) return;
    var none = null;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase(), shown = 0, rows = table.querySelectorAll("tbody tr");
      rows.forEach(function (tr) {
        if (tr.querySelector(".empty-cell") || tr.hasAttribute("data-no-match")) return;
        var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
        tr.style.display = hit ? "" : "none";
        if (hit) shown++;
      });
      if (!none) {
        none = document.createElement("tr"); none.setAttribute("data-no-match", "");
        var td = document.createElement("td"); td.className = "empty-cell"; td.colSpan = 99; none.appendChild(td);
        table.tBodies[0].appendChild(none);
      }
      none.firstChild.textContent = "No rows match “" + input.value.trim() + "”.";
      none.style.display = q && !shown ? "" : "none";
    });
  });
  // "/" focuses the first search box (not while typing in a field)
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey) return;
    var t = e.target.tagName; if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || e.target.isContentEditable) return;
    var s = document.querySelector("input.search"); if (s) { e.preventDefault(); s.focus(); }
  });

  // Loading state on submit (prevents double submits on slow tablets)
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var b = e.submitter || e.target.querySelector("button[type=submit]");
    if (b && !b.hasAttribute("data-no-loading")) { setTimeout(function () { b.classList.add("is-loading"); b.setAttribute("aria-busy", "true"); }, 0); }
  });
  window.addEventListener("pageshow", function () { document.querySelectorAll(".is-loading").forEach(function (b) { b.classList.remove("is-loading"); b.removeAttribute("aria-busy"); }); });

  // Live tolerance readout on measurement inputs. Display only: the server decides PASS/FAIL.
  // <input data-lsl data-usl data-nom data-scale="id" data-hint="id" data-card="id">
  document.querySelectorAll("input[data-lsl][data-usl]").forEach(function (input) {
    var out = document.getElementById(input.getAttribute("data-hint"));
    var scale = document.getElementById(input.getAttribute("data-scale"));
    var card = document.getElementById(input.getAttribute("data-card"));
    var lo = parseFloat(input.dataset.lsl), hi = parseFloat(input.dataset.usl);
    input.addEventListener("input", function () {
      var v = parseFloat(input.value);
      var state = isNaN(v) ? "" : (v >= lo && v <= hi ? "in" : "out");
      if (out) {
        out.className = "verdict" + (state ? " " + (state === "in" ? "pass" : "fail") : "");
        out.textContent = state === "in" ? "In tolerance" : state === "out" ? "Out of tolerance" : "";
      }
      if (scale) {
        scale.classList.remove("in", "out"); if (state) scale.classList.add(state);
        if (state && hi > lo) {
          var pos = 25 + 50 * (v - lo) / (hi - lo); pos = Math.max(2, Math.min(98, pos));
          scale.style.setProperty("--pos", pos + "%");
          var dev = scale.querySelector("[data-dev]");
          if (dev) { var nom = parseFloat(input.dataset.nom); if (!isNaN(nom)) { var d = v - nom; dev.textContent = (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(4); } }
        }
      }
      if (card) { card.classList.remove("pass", "fail"); if (state) card.classList.add(state === "in" ? "pass" : "fail"); }
    });
  });
})();

/* Segmented filters: <div data-seg-filter="table-id"><button data-seg="CATEGORY|all" aria-pressed>…; rows carry data-cat */
(function () {
  "use strict";
  document.querySelectorAll("[data-seg-filter]").forEach(function (group) {
    var table = document.getElementById(group.getAttribute("data-seg-filter"));
    if (!table) return;
    var buttons = group.querySelectorAll("button[data-seg]");
    function apply(key) {
      buttons.forEach(function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-seg") === key ? "true" : "false"); });
      var shown = 0;
      table.querySelectorAll("tbody tr[data-cat]").forEach(function (tr) {
        var hit = key === "all" || tr.getAttribute("data-cat") === key;
        tr.style.display = hit ? "" : "none"; if (hit) shown++;
      });
      var none = table.querySelector("tr[data-seg-empty]");
      if (none) none.style.display = shown ? "none" : "";
    }
    buttons.forEach(function (b) { b.addEventListener("click", function () { apply(b.getAttribute("data-seg")); }); });
  });
})();

/* Optional auto-refresh for live boards: <button data-autorefresh="30">; the choice is remembered per browser */
(function () {
  "use strict";
  var btn = document.querySelector("[data-autorefresh]");
  if (!btn) return;
  var secs = parseInt(btn.getAttribute("data-autorefresh"), 10) || 30, timer = null, key = "fqc-autorefresh";
  function set(on) {
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.classList.toggle("is-on", on);
    clearTimeout(timer);
    if (on) timer = setTimeout(function () { location.reload(); }, secs * 1000);
    try { localStorage.setItem(key, on ? "1" : "0"); } catch (e) {}
  }
  var saved = false; try { saved = localStorage.getItem(key) === "1"; } catch (e) {}
  set(saved);
  btn.addEventListener("click", function () { set(btn.getAttribute("aria-pressed") !== "true"); });
})();
