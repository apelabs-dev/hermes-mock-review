/* mockreview-probe: injected at the end of every variant page.
 * Runs inside the sandboxed (opaque-origin) iframe. It only talks to the board
 * via postMessage: it reports page size and comment-mode taps, and draws pin
 * markers (numbers + positions only, never comment text) in a closed shadow root.
 */
(function () {
  if (window.__mrProbe) return;
  window.__mrProbe = 1;
  var P = window.parent;
  if (!P || P === window) return;
  var comment = false, pins = [], host = null, root = null, k = 1;

  function post(m) { m.__mockreview = 1; try { P.postMessage(m, '*'); } catch (e) {} }
  function size() {
    var d = document.documentElement, b = document.body;
    return { w: Math.max(d.scrollWidth, b ? b.scrollWidth : 0, 1), h: Math.max(d.scrollHeight, b ? b.scrollHeight : 0, 1) };
  }
  function hello() { var s = size(); post({ type: 'hello', w: s.w, h: s.h, vw: innerWidth, vh: innerHeight }); }
  function num(v, lo, hi) { v = +v; return isFinite(v) ? Math.min(Math.max(v, lo), hi) : lo; }

  var CSS = ':host{all:initial}' +
    '.p{all:initial;position:absolute;box-sizing:border-box;width:28px;height:28px;margin:-28px 0 0 0;border-radius:14px 14px 14px 3px;' +
    'background:#ff5b3a;color:#fff;border:2px solid #fff;font:700 12px/24px -apple-system,system-ui,sans-serif;text-align:center;' +
    'box-shadow:0 2px 8px rgba(0,0,0,.35);cursor:pointer;pointer-events:auto;transition:transform .15s}' +
    '.p:hover{transform:scale(1.12)}.p.g{background:#5b5bf0;animation:mrp 1s ease-in-out infinite}' +
    '.p.f{animation:mrf .7s ease-out 2}@keyframes mrp{50%{transform:scale(1.15)}}@keyframes mrf{50%{transform:scale(1.5)}}';

  function ensureHost() {
    if (host && host.isConnected) return;
    host = document.createElement('mr-overlay');
    host.setAttribute('style', 'all:initial;position:absolute;left:0;top:0;width:0;height:0;overflow:visible;z-index:2147483647;pointer-events:none');
    root = host.attachShadow ? host.attachShadow({ mode: 'closed' }) : host;
    document.documentElement.appendChild(host);
  }
  function draw() {
    ensureHost();
    var s = size();
    while (root.firstChild) root.removeChild(root.firstChild);
    var st = document.createElement('style');
    st.textContent = CSS;
    root.appendChild(st);
    pins.forEach(function (p) {
      var b = document.createElement('button');
      b.className = 'p' + (p.ghost ? ' g' : '');
      b.textContent = String(p.n);
      var z = Math.round(28 * k);
      b.style.width = z + 'px'; b.style.height = z + 'px'; b.style.marginTop = (-z) + 'px';
      b.style.borderRadius = (z / 2) + 'px ' + (z / 2) + 'px ' + (z / 2) + 'px ' + Math.round(3 * k) + 'px';
      b.style.borderWidth = Math.max(2, Math.round(2 * k)) + 'px';
      b.style.fontSize = Math.round(12 * k) + 'px'; b.style.lineHeight = (z - 2 * Math.max(2, Math.round(2 * k))) + 'px';
      b.style.left = (p.x * s.w) + 'px';
      b.style.top = (p.y * s.h) + 'px';
      b.setAttribute('data-i', String(p.i));
      b.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        var r = b.getBoundingClientRect();
        post({ type: 'pin', i: p.i, cx: r.left + 4, cy: r.bottom });
      });
      root.appendChild(b);
    });
  }
  function fromUs(e) { return host && e.composedPath && e.composedPath().indexOf(host) >= 0; }
  // Decision-page controls (Pick / Approve / Change / Skip / note / A|B|C toggle) must work in Comment mode too:
  // a page marks them `[data-mr-live]`, and taps on them never become pins.
  function live(e) { return e.target && e.target.closest && e.target.closest('[data-mr-live]'); }

  addEventListener('click', function (e) {
    if (!comment || fromUs(e) || live(e)) return;
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
    var s = size(), px = e.clientX + scrollX, py = e.clientY + scrollY;
    post({ type: 'click', x: px / s.w, y: py / s.h, h: s.h, cx: e.clientX, cy: e.clientY });
  }, true);
  ['mousedown', 'auxclick', 'dblclick', 'submit', 'contextmenu'].forEach(function (t) {
    addEventListener(t, function (e) { if (comment && !fromUs(e) && !live(e)) { e.preventDefault(); e.stopPropagation(); } }, true);
  });

  var cursor = document.createElement('style');
  cursor.textContent = 'html.mr-comment,html.mr-comment *{cursor:crosshair!important}html.mr-comment [data-mr-live],html.mr-comment [data-mr-live] *{cursor:auto!important}';
  (document.head || document.documentElement).appendChild(cursor);

  addEventListener('message', function (e) {
    if (e.source !== P) return;
    var d = e.data;
    if (!d || d.__mockreview !== 1) return;
    if (d.cmd === 'state') {
      comment = !!d.comment;
      k = 1 / num(d.scale, 0.2, 1);
      document.documentElement.classList.toggle('mr-comment', comment);
      pins = (Array.isArray(d.pins) ? d.pins : []).slice(0, 60).map(function (p) {
        return { i: num(p.i, -1, 99) | 0, n: num(p.n, 0, 99) | 0, x: num(p.x, 0, 1), y: num(p.y, 0, 1), ghost: !!p.ghost };
      });
      draw();
    } else if (d.cmd === 'focus') {
      var p = pins.filter(function (q) { return q.i === d.i; })[0];
      if (!p) return;
      var s = size();
      scrollTo(0, Math.max(0, p.y * s.h - innerHeight / 2));
      setTimeout(function () {
        var b = root && root.querySelector('[data-i="' + p.i + '"]');
        if (!b) return;
        b.classList.remove('f'); void b.offsetWidth; b.classList.add('f');
        var r = b.getBoundingClientRect();
        post({ type: 'pin', i: p.i, cx: r.left + 4, cy: r.bottom });
      }, 80);
    }
  });

  var t = null;
  function changed() { clearTimeout(t); t = setTimeout(function () { hello(); if (pins.length) draw(); }, 120); }
  if (window.ResizeObserver) new ResizeObserver(changed).observe(document.documentElement);
  addEventListener('load', hello);
  hello();
})();
