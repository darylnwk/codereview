(function () {
  // Example data only. Nothing here calls the API.
  var STORAGE = "reviewgrade.account.v2";
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var blank = {
    email: "ada@example.com",
    plan: "month",            // "trial" | "month"
    cap: 50,
    finished: 36,
    included: 250,
    keys: [
      { name: "ci", prefix: "ra_live_7kQm3aP1", last: "2 Oct", revoked: false },
      { name: "agent", prefix: "ra_live_p2nR8cQw", last: "", revoked: false }
    ]
  };
  var RUNS = [
    { id: "run_01J9X4EXAMPLE", source: "agent", status: "done", when: "12 Oct 10:02" },
    { id: "run_01J9X3EXAMPLE", source: "agent", status: "done", when: "12 Oct 09:58" },
    { id: "run_01J9WZEXAMPLE", source: "ci", status: "failed", when: "11 Oct 17:40" },
    { id: "run_01J9WREXAMPLE", source: "ci", status: "done", when: "11 Oct 17:12" },
    { id: "run_01J9VKEXAMPLE", source: "api", status: "done", when: "10 Oct 14:31" },
    { id: "run_01J9TAEXAMPLE", source: "web", status: "done", when: "9 Oct 11:05" }
  ];
  var PLANS = {
    month: { included: 250, tag: "Month", line: "$25 for 250 reviews, $0.10 each. Then $0.15 a review.", bill: "Month plan, $25 for 250 reviews. Renews [renewal date]. Payment and invoices are handled by Stripe.", finished: 36 },
    trial: { included: 20, tag: "Trial", line: "20 free reviews, once per email. No extra reviews on the trial.", bill: "Trial. 20 reviews, once per email. Upgrade to the Month for 250 reviews at $0.10 each.", finished: 6 }
  };

  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function load() {
    try {
      var d = JSON.parse(sessionStorage.getItem(STORAGE) || "null");
      if (!d || typeof d.cap !== "number" || !Array.isArray(d.keys) || !PLANS[d.plan]) return clone(blank);
      return d;
    } catch (e) { return clone(blank); }
  }
  function save() { try { sessionStorage.setItem(STORAGE, JSON.stringify(state)); } catch (e) {} }
  function token(n) {
    var a = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789", b = new Uint8Array(n), o = "";
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(b); else for (var j = 0; j < n; j++) b[j] = Math.floor(Math.random() * 256);
    for (var i = 0; i < n; i++) o += a[b[i] % a.length];
    return o;
  }
  function $(s) { return document.querySelector(s); }
  function money(n) { return "$" + n.toFixed(2); }
  function note(id, t) { var el = $(id); el.textContent = t; el.hidden = t === ""; }

  var state = load();
  var reveal = "", newPrefix = "", counted = false;

  function left() { return Math.max(0, state.included - state.finished); }
  function capLine() {
    return state.plan === "trial" ? "The trial has no extra reviews. The cap applies once you are on the Month."
      : "After 250 reviews, each one is $0.15 until the extra charge reaches $" + state.cap + ".";
  }
  function extra() { return Math.max(0, state.finished - 250) * 0.15; }

  function countUp(el, to) {
    if (reduce || counted) { el.textContent = String(to); return; }
    counted = true;
    var t0 = performance.now(), dur = 900;
    (function f(t) {
      var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 4);
      el.textContent = String(Math.round(to * e));
      if (p < 1) requestAnimationFrame(f);
    })(t0);
  }

  function cell(text, cls) { var td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; return td; }

  function render() {
    var plan = PLANS[state.plan];
    $("#email").textContent = state.email;
    countUp($("#left"), left());
    $("#plan-tag").textContent = plan.tag;
    $("#plan-line").textContent = plan.line;
    $("#used").textContent = state.finished;
    $("#included").textContent = state.included;
    $("#used-line").textContent = state.finished + " of " + state.included + " are finished. Unused reviews end with the month.";
    requestAnimationFrame(function () { $("#used-bar").style.transform = "scaleX(" + (state.finished / state.included) + ")"; });
    var base = state.plan === "month" ? 25 : 0;
    $("#spend").textContent = money(base + extra());
    $("#spend-line").textContent = state.plan === "month" ? money(base) + " for the month, plus " + money(extra()) + " over 250 (cap $" + state.cap + ")." : "Nothing spent. The trial is free.";
    var capInput = $("#cap");
    if (document.activeElement !== capInput) capInput.value = String(state.cap);
    capInput.disabled = state.plan === "trial";
    $("#cap-line").textContent = capLine();
    $("#bill-line").textContent = plan.bill;
    $("#upgrade").hidden = state.plan !== "trial";
    $("#plan-toggle").textContent = state.plan === "trial" ? "Preview as Month" : "Preview as Trial";

    // Ledger
    var tb = $("#runs"); tb.replaceChildren();
    RUNS.forEach(function (r) {
      var tr = document.createElement("tr");
      var st = cell(r.status === "done" ? "done" : "failed", "st st--" + r.status);
      var cost = r.status === "failed" ? "No charge" : (state.plan === "month" ? "1 credit · $0.10" : "1 trial credit");
      tr.append(cell(r.id, "mono"), cell(r.source, "mono dim"), st, cell(r.when, "dim"), cell(cost, "r mono" + (r.status === "failed" ? " dim" : "")));
      tb.append(tr);
    });

    // Keys
    var list = $("#key-list"); list.replaceChildren();
    state.keys.forEach(function (key, index) {
      var tr = document.createElement("tr");
      if (key.revoked) tr.className = "is-revoked";
      if (key.prefix === newPrefix) tr.className = "is-new";
      tr.append(cell(key.name), cell(key.prefix + "…", "mono dim"), cell(key.revoked ? "Revoked" : (key.last ? key.last : "Not used yet"), "dim"));
      var td = document.createElement("td"); td.className = "r";
      if (!key.revoked) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "btn btn--quiet link"; b.textContent = "Revoke";
        b.setAttribute("aria-label", "Revoke key " + key.name);
        b.addEventListener("click", function () {
          state.keys[index].revoked = true;
          if (reveal.indexOf(key.prefix) === 0) reveal = "";
          newPrefix = ""; save(); render();
        });
        td.append(b);
      }
      tr.append(td); list.append(tr);
    });

    var secret = $("#secret");
    secret.hidden = !reveal;
    $("#secret-value").textContent = reveal;
  }

  var capInput = $("#cap");
  function readCap(restore) {
    var raw = capInput.value.trim();
    if (!/^\d+$/.test(raw)) {
      if (restore) { capInput.value = String(state.cap); note("#cap-note", ""); }
      else note("#cap-note", raw === "" ? "" : "Use a whole number of dollars, from 0 up.");
      return;
    }
    note("#cap-note", "");
    state.cap = Number(raw); save();
    $("#cap-line").textContent = capLine();
  }
  capInput.addEventListener("input", function () { readCap(false); });
  capInput.addEventListener("blur", function () { readCap(true); });
  $("#cap-form").addEventListener("submit", function (e) { e.preventDefault(); capInput.blur(); });

  $("#key-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var name = $("#key-name").value.trim();
    if (!name) { note("#key-note", "Name the key."); return; }
    note("#key-note", "");
    var secret = "ra_live_" + token(24);
    newPrefix = secret.slice(0, 16);
    state.keys.push({ name: name, prefix: newPrefix, last: "", revoked: false });
    reveal = secret;
    $("#key-name").value = ""; $("#copy-note").textContent = ""; $("#secret-copy").classList.remove("is-copied"); $("#copy-label").textContent = "Copy";
    save(); render();
    $("#secret-copy").focus({ preventScroll: true });
  });

  $("#secret-copy").addEventListener("click", function () {
    var btn = this;
    function done(ok) {
      btn.classList.remove("is-copied"); void btn.offsetWidth; btn.classList.add("is-copied");
      $("#copy-label").textContent = ok ? "Copied" : "Select and copy";
      $("#copy-note").textContent = ok ? "Copied to clipboard." : "Clipboard blocked. Select the secret and copy it.";
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(reveal).then(function () { done(true); }, function () { done(false); });
    else done(false);
  });
  $("#secret-hide").addEventListener("click", function () { reveal = ""; newPrefix = ""; render(); });

  $("#plan-toggle").addEventListener("click", function () {
    state.plan = state.plan === "trial" ? "month" : "trial";
    state.included = PLANS[state.plan].included; state.finished = PLANS[state.plan].finished;
    counted = false; save(); render();
  });
  $("#sign-out").addEventListener("click", function () { $("#signout-note").hidden = false; });
  ["#upgrade", "#manage"].forEach(function (s) { $(s).addEventListener("click", function (e) { e.preventDefault(); }); });

  // Staggered reveal
  var rv = document.querySelectorAll(".rv");
  if (reduce || !("IntersectionObserver" in window)) rv.forEach(function (el) { el.classList.add("in"); });
  else {
    var io = new IntersectionObserver(function (es) { es.forEach(function (x) { if (x.isIntersecting) { x.target.classList.add("in"); io.unobserve(x.target); } }); }, { threshold: 0.1 });
    rv.forEach(function (el) { io.observe(el); });
  }

  render();
})();
