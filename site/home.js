(function () {
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Scroll reveals
  var rv = document.querySelectorAll('.rv');
  if (reduce || !('IntersectionObserver' in window)) {
    rv.forEach(function (el) { el.classList.add('in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.12 });
    rv.forEach(function (el) { io.observe(el); });
  }

  // Hero: the diff reviews itself
  var diff = document.getElementById('hero-diff');
  if (!diff) return;
  var head = diff.querySelector('.readhead');
  var lines = Array.prototype.slice.call(diff.querySelectorAll('.dl'));
  var status = document.getElementById('hero-status');
  var findings = diff.querySelectorAll('.finding');
  var timers = [];
  function at(ms, fn) { timers.push(setTimeout(fn, ms)); }
  function finding(n) { return diff.querySelector('.finding[data-for="' + n + '"]'); }

  function finalState() {
    diff.dataset.state = 'done';
    status.textContent = 'done · 2 findings';
    lines.forEach(function (l) { if (l.dataset.finding) l.classList.add('is-flagged'); });
    findings.forEach(function (f) { f.classList.add('in'); });
  }
  if (reduce) { finalState(); return; }

  var STEP = 260; // ms per line
  function run() {
    timers.forEach(clearTimeout); timers = [];
    diff.dataset.state = 'idle';
    status.textContent = 'queued';
    lines.forEach(function (l) { l.classList.remove('is-flagged'); });
    findings.forEach(function (f) { f.classList.remove('in', 'lit'); });
    head.style.transition = 'none';
    head.style.transform = 'translateY(' + lines[0].offsetTop + 'px)';

    at(700, function () { diff.dataset.state = 'reviewing'; status.textContent = 'reviewing…'; });
    lines.forEach(function (l, i) {
      at(900 + i * STEP, function () {
        head.style.transition = 'transform ' + STEP + 'ms cubic-bezier(0.65,0,0.35,1)';
        head.style.transform = 'translateY(' + l.offsetTop + 'px)';
      });
      if (l.dataset.finding) {
        var n = l.dataset.finding;
        at(900 + i * STEP + STEP, function () { l.classList.add('is-flagged'); });
        at(900 + i * STEP + STEP + 380, function () { var f = finding(n); f.classList.add('in', 'lit'); setTimeout(function () { f.classList.remove('lit'); }, 900); });
      }
    });
    var end = 900 + lines.length * STEP + 500;
    at(end, function () { diff.dataset.state = 'done'; status.textContent = 'done · 2 findings'; });
    at(end + 6500, run);
  }

  var started = false;
  var vis = new IntersectionObserver(function (es) {
    es.forEach(function (e) {
      if (e.isIntersecting && !started) { started = true; run(); }
      else if (!e.isIntersecting && started) { started = false; timers.forEach(clearTimeout); finalState(); }
    });
  }, { threshold: 0.3 });
  vis.observe(diff);
  findings.forEach(function (f) {
    var line = diff.querySelector('.dl[data-finding="' + f.dataset.for + '"]');
    f.addEventListener('mouseenter', function () { line.classList.add('pulse'); });
    f.addEventListener('mouseleave', function () { line.classList.remove('pulse'); });
  });
})();
