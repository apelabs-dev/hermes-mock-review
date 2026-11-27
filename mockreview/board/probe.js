import {LIVE, CONTROL, controlOf, presses, anchorFor, cleanTarget, surface, installAPI} from './anchors.js';
/* mockreview-probe: injected into every variant page (the top of an artifact page, the end of the others).
 * Runs inside the sandboxed (opaque-origin) iframe. It talks to the board only
 * via postMessage: it reports page size, page-state writes and comment-mode taps,
 * and draws pin markers (numbers + positions only, never comment text) in a
 * closed shadow root. The clipboard stays in the frame.
 */
(function () {
  if (window.__mrProbe) return;
  window.__mrProbe = 1;
  var P = window.parent;
  // <img name="body"> is a named property on the document and shadows Document.prototype.body; the probe always means
  // the real body, so it captures the getter once instead of reading document.body at call time.
  var bodyGet = Object.getOwnPropertyDescriptor(Document.prototype, 'body').get;
  function bodyOf() { return bodyGet.call(document); }
  var comment = false, pins = [], host = null, root = null, k = 1, anchors = null, t = null, sizeObserver = null;
  var tapAt = 0, state = null, readyDone = false, readyResolve = null, pending = {}, nextId = 1;
  var ready = new Promise(function (resolve) { readyResolve = resolve; });
  // Every id settles: the probe keeps its own outgoing bucket (the board's bucket drops a flood without an answer) and
  // a pending call that gets no answer in time fails with the same error instead of hanging the page's promise.
  var OUT_PER_SECOND = 40, ACK_MS = 3000, out = [];
  // The page APIs exist only in an artifact block file. A prototype or variant page gets the probe too (height, taps,
  // comments) but not these: `use()` returns null there, as it does for an unknown name. Computed once at probe start.
  var artifactPage = /\/a\/blocks\/[a-z0-9-]+\.html$/.test(location.pathname);

  function put(o, k, v) {
    Object.defineProperty(o, k, { value: v, writable: true, enumerable: true, configurable: true });
  }
  function budget() {
    var now = Date.now();
    while (out.length && now - out[0] >= 1000) out.shift();
    return out.length < OUT_PER_SECOND;
  }
  function track(id, entry) {
    entry.timer = setTimeout(function () {
      if (!pending[id]) return;
      delete pending[id];
      entry.fail(new Error('rate limited'));
    }, ACK_MS);
    pending[id] = entry;
  }
  function settle(id, ok, error) {
    var p = pending[id];
    if (!p) return;
    delete pending[id];
    clearTimeout(p.timer);
    if (ok) {
      if (p.op === 'set') { state = state || {}; put(state, p.key, p.value); }
      else if (p.op === 'remove' && state) delete state[p.key];
      p.ok(true);
    } else p.fail(new Error(error || 'refused'));
  }
  function bytes8(v) { return typeof v === 'string' ? new TextEncoder().encode(v).length : 0; }
  function count(o) { return o ? Object.keys(o).length : 0; }

  var dataAPI = Object.freeze({
    get: function (key) {
      return ready.then(function () { return state && Object.prototype.hasOwnProperty.call(state, key) ? state[key] : undefined; });
    },
    set: function (key, value) {
      return new Promise(function (ok, fail) {
        if (typeof key !== 'string') return fail(new Error('key must be a string'));
        var s;
        try { s = JSON.stringify(value); } catch (e) { s = null; }
        if (typeof s !== 'string') return fail(new Error('value is not JSON'));
        if (bytes8(s) > 8192) return fail(new Error('value over 8 KB'));
        var cur = state || {};
        if (!Object.prototype.hasOwnProperty.call(cur, key) && count(cur) >= 64) return fail(new Error('block has 64 keys'));
        if (!budget()) return fail(new Error('rate limited'));
        var id = nextId++, v = JSON.parse(s);
        track(id, { ok: ok, fail: fail, op: 'set', key: key, value: v });
        post({ type: 'data', op: 'set', key: key, value: v, id: id });
      });
    },
    list: function () {
      return ready.then(function () { return state ? Object.keys(state).sort() : []; });
    },
    remove: function (key) {
      return new Promise(function (ok, fail) {
        if (typeof key !== 'string') return fail(new Error('key must be a string'));
        if (!budget()) return fail(new Error('rate limited'));
        var id = nextId++;
        track(id, { ok: ok, fail: fail, op: 'remove', key: key });
        post({ type: 'data', op: 'remove', key: key, id: id });
      });
    }
  });

  var marksAPI = Object.freeze({
    set: function (mark) {
      return new Promise(function (ok, fail) {
        var m = mark && typeof mark === 'object' ? mark : {};
        if (m.status !== 'approve' && m.status !== 'change' && m.status !== 'skip') {
          return fail(new Error('status must be approve, change or skip'));
        }
        if (!budget()) return fail(new Error('rate limited'));
        var id = nextId++, msg = { type: 'mark', status: m.status, id: id };
        // A page that sets only a status must not wipe a note the reviewer typed on the board.
        if (typeof m.note === 'string') msg.note = m.note;
        track(id, { ok: ok, fail: fail });
        post(msg);
      });
    }
  });

  var clipboardAPI = Object.freeze({
    writeText: function (text) {
      return new Promise(function (ok, fail) {
        if (typeof text !== 'string') return fail(new Error('text must be a string'));
        var age = tapAt ? Date.now() - tapAt : 1e9;
        if (age > 1000) return fail(new Error('no recent tap'));
        if (!navigator.clipboard || !navigator.clipboard.writeText) return fail(new Error('clipboard unavailable'));
        try {
          navigator.clipboard.writeText(text).then(function () { ok(true); }, function (e) {
            fail(new Error('clipboard refused: ' + ((e && e.name) || e) + ': ' + ((e && e.message) || '')));
          });
        } catch (e) { fail(new Error('clipboard refused: ' + ((e && e.name) || e))); }
      });
    }
  });

  // Capability hook for artifact pages: `window.mockreview?.use(name)`. Only the names implemented here are served,
  // and only inside an artifact block file; every other name and every other page returns null, so a page that asks
  // for something absent renders without it. installAPI adds `comments` to this same object everywhere and it is
  // frozen after that, before any page script runs.
  var hook = { use: function (name) {
    if (!artifactPage) return null;
    return name === 'data' ? dataAPI : name === 'marks' ? marksAPI : name === 'clipboard' ? clipboardAPI : null;
  } };
  try { Object.defineProperty(window, 'mockreview', { value: hook, writable: false, configurable: false, enumerable: false }); } catch (e) {}

  var canvas = false, wheelPending = null, wheelFrame = 0, middle = null, canvasSpaceDown = false;
  var canvasCount = 0, canvasSince = 0;
  function post(m) {
    if (m.type === 'canvas-wheel' || m.type === 'canvas-key') {
      var now = performance.now();
      if (now - canvasSince >= 1000) { canvasSince = now; canvasCount = 0; }
      if (++canvasCount > 144 && !(m.key === ' ' && !m.down)) return;
    } else { budget(); out.push(Date.now()); }
    m.__mockreview = 1;
    try { if (P) P.postMessage(m, '*'); } catch (e) {}
  }
  function size() {
    if (canvas) return { w: innerWidth, h: Math.min(30000, Math.round(pageHeight())) };
    var d = document.documentElement, b = bodyOf();
    return { w: Math.max(d.scrollWidth, b ? b.scrollWidth : 0, 1), h: Math.max(d.scrollHeight, b ? b.scrollHeight : 0, 1) };
  }
  // The height of the page's own content, independent of the viewport this probe runs in. The root's scrollHeight is
  // never smaller than that viewport, so a frame sized from it can grow and never shrink again. The root element's
  // border box is the page's layout height instead: body margins that collapse at the page edge are inside it and the
  // viewport clamp is not. The body's scrollHeight adds content that escapes its own box, including under a scroll
  // container (`overflow:auto|hidden`) whose `height:100%` box stays at the viewport size. An absolute box laid out
  // against the initial containing block can escape both and is counted by its own bottom edge. Fixed boxes are not
  // page content. A page that sizes itself with html/body{height:100%} or min-height:100vh reports the viewport by
  // design rather than shrinking to the smallest frame.
  function pageHeight() {
    var d = document.documentElement, b = bodyOf();
    var h = d.getBoundingClientRect().height, y = scrollY || d.scrollTop || 0;
    if (b) {
      h = Math.max(h, b.getBoundingClientRect().height, b.scrollHeight);
      function countAbsolute(c, nested) {
        var cs;
        try { cs = getComputedStyle(c); } catch (e) { return; }
        if (cs.position !== 'absolute') return;
        if (nested) {
          // offsetParent can report the static body for a viewport-anchored box, as Chrome does.
          // Leave boxes with their own containing ancestor to the normal scroll/overflow measurement.
          var p = c.offsetParent;
          if (p !== null && (p !== b || getComputedStyle(b).position !== 'static')) return;
        }
        // A display:none box (including one under a hidden ancestor) has no client rects.
        if (c.getClientRects().length) h = Math.max(h, c.getBoundingClientRect().bottom + y);
      }
      // Keep direct children first. Walk incrementally within a fixed budget, without collecting a huge NodeList.
      var limit = 4096, i = 0, c;
      for (c = b.firstElementChild; c && i < limit; c = c.nextElementSibling, i++) countAbsolute(c, false);
      if (i < limit) {
        var walk = document.createTreeWalker(b, NodeFilter.SHOW_ELEMENT);
        for (c = walk.nextNode(); c && i < limit; c = walk.nextNode(), i++) {
          if (c.parentNode !== b) countAbsolute(c, true);
        }
      }
    }
    return Math.max(h, 1);
  }
  // The page's own background, for the board's canvas around a page-mode frame. An iframe paints an opaque white
  // canvas, so a translucent or absent body colour is composited over white; a fully transparent one reports nothing
  // and the board keeps its own background.
  function bgColor() {
    function read(el) {
      if (!el) return null;
      var m, v = '';
      try { v = getComputedStyle(el).backgroundColor || ''; } catch (e) { return null; }
      if ((m = v.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i))) {
        var a = m[4] === undefined ? 1 : Math.min(1, Math.max(0, +m[4]));
        if (!a) return null;
        return { r: Math.round(+m[1] * a + 255 * (1 - a)), g: Math.round(+m[2] * a + 255 * (1 - a)),
                 b: Math.round(+m[3] * a + 255 * (1 - a)) };
      }
      if ((m = v.match(/^#([0-9a-f]{3,8})$/i))) {
        var h = m[1];
        if (h.length === 3 || h.length === 4) h = h.split('').map(function (c) { return c + c; }).join('');
        if (h.length !== 6 && h.length !== 8) return null;
        var alpha = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
        if (!alpha) return null;
        var rgb = [0, 2, 4].map(function (i) { return parseInt(h.slice(i, i + 2), 16); });
        return { r: Math.round(rgb[0] * alpha + 255 * (1 - alpha)), g: Math.round(rgb[1] * alpha + 255 * (1 - alpha)),
                 b: Math.round(rgb[2] * alpha + 255 * (1 - alpha)) };
      }
      return null;
    }
    var c = read(bodyOf()) || read(document.documentElement);
    return c ? 'rgb(' + c.r + ', ' + c.g + ', ' + c.b + ')' : '';
  }
  function hello() {
    var s = size();
    post({ type: 'hello', w: s.w, h: pageHeight(), vw: innerWidth, vh: innerHeight, bg: bgColor() });
  }
  function num(v, lo, hi) { v = +v; return isFinite(v) ? Math.min(Math.max(v, lo), hi) : lo; }

  var CSS = ':host{all:initial}' +
    '.p{all:initial;position:absolute;box-sizing:border-box;width:28px;height:28px;margin:-28px 0 0 0;border-radius:14px 14px 14px 3px;' +
    'background:#5b5bf0;color:#fff;border:2px solid #fff;font:700 12px/24px -apple-system,system-ui,sans-serif;text-align:center;' +
    'box-shadow:0 2px 8px rgba(0,0,0,.35);cursor:pointer;pointer-events:auto;transition:transform .15s}' +
    '.p:hover{transform:scale(1.12)}.p.g{background:#5b5bf0;animation:mrp 1s ease-in-out infinite}' +
    '.p.det{background:#5b5bf0;opacity:.55}.p.f{animation:mrf .7s ease-out 2}@keyframes mrp{50%{transform:scale(1.15)}}@keyframes mrf{50%{transform:scale(1.5)}}' +
    '.p .hermes-pin-indicator{position:absolute;pointer-events:none;box-sizing:border-box;border-radius:50%}' +
    '.p.h-working .hermes-pin-indicator{right:-3px;top:-3px;width:10px;height:10px;background:#dc2626;border:2px solid #fff;animation:mrpulse 1.2s ease-in-out infinite}' +
    '.p.h-failed .hermes-pin-indicator{right:-5px;bottom:-5px;width:16px;height:16px;background:#fff;border:1px solid #dc2626;color:#dc2626;line-height:14px}' +
    '@keyframes mrpulse{0%,100%{transform:scale(1);box-shadow:0 0 0 0 rgba(220,38,38,.55)}50%{transform:scale(1.25);box-shadow:0 0 0 5px rgba(220,38,38,0)}}@media(prefers-reduced-motion:reduce){.p.h-working .hermes-pin-indicator{animation:none}}';

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
      b.className = 'p' + (p.ghost ? ' g' : '') + (p.detached ? ' det' : '');
      if (p.detached) b.title = 'Element removed';
      b.textContent = String(p.n);
      if (p.hermes) {
        b.classList.add('h-' + p.hermes);
        b.setAttribute('aria-label', 'Pin ' + p.n + (p.hermes === 'working' ? (p.queued ? ' · Queued behind ' + p.queued : ' · Hermes is working') : ' · Hermes couldn’t finish'));
        var indicator = document.createElement('span');
        indicator.className = 'hermes-pin-indicator';
        indicator.setAttribute('aria-hidden', 'true');
        if (p.hermes === 'failed') {
          var ns = 'http://www.w3.org/2000/svg';
          var cross = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
          cross.setAttribute('width', '12');
          cross.setAttribute('height', '12');
          cross.setAttribute('viewBox', '0 0 12 12');
          path.setAttribute('d', 'M3 3 9 9 M9 3 3 9');
          path.setAttribute('stroke', 'currentColor');
          cross.appendChild(path);
          indicator.appendChild(cross);
        }
        b.appendChild(indicator);
      }
      var z = Math.round(28 * k);
      b.style.width = z + 'px'; b.style.height = z + 'px'; b.style.marginTop = (-z) + 'px';
      b.style.borderRadius = (z / 2) + 'px ' + (z / 2) + 'px ' + (z / 2) + 'px ' + Math.round(3 * k) + 'px';
      b.style.borderWidth = Math.max(2, Math.round(2 * k)) + 'px';
      b.style.fontSize = Math.round(12 * k) + 'px'; b.style.lineHeight = (z - 2 * Math.max(2, Math.round(2 * k))) + 'px';
      var ax = p.detached && p.target ? p.target.doc.x : p.x * s.w;
      var ay = p.detached && p.target ? p.target.doc.y : p.y * s.h;
      b.style.left = ax + 'px';
      b.style.top = ay + 'px';
      // A marker hangs above its anchor; too close to the page's top edge it would be clipped, so it hangs below.
      if (ay < z + 2) { b.style.marginTop = '0px'; b.style.borderRadius = Math.round(3 * k) + 'px ' + (z / 2) + 'px ' + (z / 2) + 'px ' + (z / 2) + 'px'; }
      b.setAttribute('data-i', String(p.i));
      b.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        var r = b.getBoundingClientRect();
        post({ type: 'pin', i: p.i, thread: p.thread, cx: r.left + 4, cy: r.bottom });
      });
      root.appendChild(b);
    });
  }
  function fromUs(e) { return host && e.composedPath && e.composedPath().indexOf(host) >= 0; }
  function tap(e) { if (e.isTrusted) tapAt = Date.now(); }
  addEventListener('pointerdown', tap, true);
  addEventListener('click', tap, true);

  // Comment mode pins a click on anything in the page, its buttons, links and fields included. Only LIVE keeps its clicks:
  // the controls a page marks `[data-mr-live]` (Pick / Approve / Change / Skip / note / A|B|C toggle) work in comment
  // mode too and never become pins. To use any other control, the reviewer leaves comment mode.
  function pathOf(e) { return e.composedPath ? e.composedPath() : [e.target]; }
  function live(e) {
    return pathOf(e).some(function (el) { return el instanceof Element && Element.prototype.closest.call(el, LIVE); });
  }
  function inControl(e) {
    return pathOf(e).some(function (el) { return el instanceof Element && Element.prototype.matches.call(el, CONTROL); });
  }
  function pinning(e) { return comment && !fromUs(e) && !live(e); }

  function flushCanvasWheel() {
    var d = wheelPending;
    wheelPending = null;
    if (canvas && d) post(d);
  }
  function queueCanvasWheel(e, dx, dy, ctrl, meta, drag) {
    // Don't merge a pan with a zoom when a modifier changes between adjacent events.
    if (wheelPending && ((wheelPending.ctrlKey || wheelPending.metaKey) !== (ctrl || meta) || wheelPending.drag !== !!drag)) flushCanvasWheel();
    var p = wheelPending || (wheelPending = {type: 'canvas-wheel', deltaX: 0, deltaY: 0});
    p.deltaX = num(p.deltaX + dx, -1200, 1200); p.deltaY = num(p.deltaY + dy, -1200, 1200);
    p.ctrlKey = ctrl; p.metaKey = meta; p.drag = !!drag; p.clientX = e.clientX; p.clientY = e.clientY;
    if (!wheelFrame) wheelFrame = requestAnimationFrame(function () { wheelFrame = 0; flushCanvasWheel(); });
  }
  function canvasWheel(e) {
    if (!e.isTrusted) return;
    e.preventDefault(); e.stopImmediatePropagation();
    var mode = e.deltaMode, unit = mode === 1 ? 16 : mode === 2 ? innerHeight : 1;
    queueCanvasWheel(e, e.deltaX * unit, e.deltaY * unit, e.ctrlKey, e.metaKey);
  }
  function canvasKeys(e) {
    var key = e.key, down = e.type === 'keydown';
    if (!canvas || !e.isTrusted || (key !== ' ' && key !== '+' && key !== '-' && key !== '0' && key !== '1')) return;
    if (!(key === ' ' && !down && canvasSpaceDown) && (e.isComposing || e.ctrlKey || e.metaKey || e.altKey ||
        pathOf(e).some(function (n) { return n.isContentEditable || (n instanceof Element && n.matches('input,textarea,select')); }))) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (key === ' ') {
      if (canvasSpaceDown === down) return;
      canvasSpaceDown = down;
    }
    post({type: 'canvas-key', key: key, down: down});
  }
  addEventListener('keydown', canvasKeys, true);
  addEventListener('keyup', canvasKeys, true);
  addEventListener('blur', function () {
    if (!canvasSpaceDown) return;
    canvasSpaceDown = false;
    post({type: 'canvas-key', key: ' ', down: false});
  });
  // The middle press starts inside the iframe. Capture it there and use screen deltas, since the iframe itself moves.
  addEventListener('pointerdown', function (e) {
    if (!canvas || !e.isTrusted || e.button !== 1) return;
    e.preventDefault(); e.stopImmediatePropagation();
    e.target.setPointerCapture(e.pointerId);
    middle = {id: e.pointerId, x: e.screenX, y: e.screenY};
  }, true);
  addEventListener('pointermove', function (e) {
    if (!middle || e.pointerId !== middle.id || !e.isTrusted || !(e.buttons & 4)) return;
    e.stopImmediatePropagation();
    queueCanvasWheel(e, middle.x - e.screenX, middle.y - e.screenY, false, false, true);
    middle.x = e.screenX; middle.y = e.screenY;
  }, true);
  addEventListener('lostpointercapture', function (e) { if (middle && middle.id === e.pointerId) middle = null; }, true);
  addEventListener('auxclick', function (e) {
    if (canvas && e.button === 1) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  // A comment-mode press belongs to the pin, so the page's own handlers never see it. These are window capture listeners
  // and run before any page listener on the document or an element. An artifact page's probe is its first script, so
  // they also run before the page's own window listeners; a prototype page gets the probe at its end, so a page listener
  // registered on window in the capture phase before the probe still runs first. `tap` above was registered first and
  // keeps running. mousedown is cancelled too inside a native control (no focus, no select opening, no range drag), and
  // only there: cancelling it on ordinary content would stop text selection, and with it quote pins.
  function hold(e) {
    if (!pinning(e)) return;
    e.stopImmediatePropagation();
    if (e.type === 'mousedown' && inControl(e)) e.preventDefault();
  }
  ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'dblclick', 'auxclick'].forEach(function (type) {
    addEventListener(type, hold, true);
  });
  ['touchstart', 'touchend'].forEach(function (type) { addEventListener(type, hold, { capture: true, passive: true }); });

  addEventListener('click', function (e) {
    if (!pinning(e)) return;
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
    var s = size(), px = e.clientX + scrollX, py = e.clientY + scrollY;
    // A click inside a native control pins the control. Its mousedown was cancelled, so a selection still showing is an
    // older one, not this click's.
    var control = inControl(e), selection = control ? null : getSelection();
    var range = selection && !selection.isCollapsed && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
    var target = anchorFor(controlOf(e.target, bodyOf()), bodyOf(), e.clientX, e.clientY, range);
    post({ type: 'click', x: px / s.w, y: py / s.h, h: s.h, cx: e.clientX, cy: e.clientY, target: target });
  }, true);
  // Enter or Space on a focused native control would activate it; in comment mode it does nothing and the page's own key
  // handlers do not see it. A key makes no pin. Escape (below) and every other key pass through.
  function keys(e) {
    if (!pinning(e) || !presses(e.key, pathOf(e)[0])) return;
    e.preventDefault(); e.stopImmediatePropagation();
  }
  addEventListener('keydown', keys, true);
  addEventListener('keyup', keys, true);
  addEventListener('keydown', function (e) { if (e.key === 'Escape' && comment) post({ type: 'escape' }); });
  // The pin cursor over everything a comment-mode click pins, controls included; LIVE elements keep their own.
  var cursor = document.createElement('style');
  cursor.textContent = 'html.mr-canvas{overflow:hidden!important}' +
    'html.mr-comment,html.mr-comment *{cursor:crosshair!important}html.mr-comment :is(' + LIVE + '),html.mr-comment :is(' + LIVE + ') *{cursor:auto!important}';
  (document.head || document.documentElement).appendChild(cursor);

  addEventListener('message', function (e) {
    if (e.source !== P) return;
    var d = e.data;
    if (!d || d.__mockreview !== 1) return;
    if (d.cmd === 'ping') {   // the board asks "is this the block file?": only a page with this probe answers
      hello();
    } else if (d.cmd === 'data') {
      state = d.state && typeof d.state === 'object' && !Array.isArray(d.state) ? d.state : {};
      if (!readyDone) { readyDone = true; if (readyResolve) readyResolve(); }
    } else if (d.cmd === 'data-ack' || d.cmd === 'mark-ack') {
      settle(d.id, !!d.ok, d.error);
    } else if (d.cmd === 'state') {
      comment = !!d.comment;
      if (!comment && anchors) anchors.clearHover();
      var canvasNext = artifactPage && d.canvas === true;
      if (canvasNext !== canvas) {
        canvas = canvasNext;
        document.documentElement.classList.toggle('mr-canvas', canvas);
        if (canvas) addEventListener('wheel', canvasWheel, {capture: true, passive: false});
        else removeEventListener('wheel', canvasWheel, true);
      }
      k = 1 / (canvas ? num(d.scale, 0.15, 2.5) : num(d.scale, 0.2, 1));
      document.documentElement.classList.toggle('mr-comment', comment);
      pins = (Array.isArray(d.pins) ? d.pins : []).slice(0, 60).map(function (p) {
        return { i: num(p.i, -1, 99) | 0, n: num(p.n, 0, 99) | 0, x: num(p.x, 0, 1), y: num(p.y, 0, 1), ghost: !!p.ghost, thread: typeof p.thread === 'string' ? p.thread : '', detached: !!p.detached,
          hermes: p.hermes === 'working' || p.hermes === 'failed' ? p.hermes : '',
          queued: p.hermes === 'working' ? num(p.queued, 0, 99) | 0 : 0,
          ...(cleanTarget(p.target) ? {target: cleanTarget(p.target)} : {}) };
      });
      draw(); if (anchors) anchors.schedule();
    } else if (d.cmd === 'focus' && !canvas) {
      var p = pins.filter(function (q) { return q.i === d.i; })[0];
      if (!p) return;
      var s = size();
      scrollTo(0, Math.max(0, p.y * s.h - innerHeight / 2));
      setTimeout(function () {
        var b = root && root.querySelector('[data-i="' + p.i + '"]');
        if (!b) return;
        b.classList.remove('f'); void b.offsetWidth; b.classList.add('f');
        var r = b.getBoundingClientRect();
        post({ type: 'pin', i: p.i, thread: p.thread, cx: r.left + 4, cy: r.bottom });
      }, 80);
    }
  });

  // Synchronous phase: the API exists and is frozen before the page's own scripts run. The scope stays empty until
  // the parser has made a body (an artifact page's probe is its first script, before <body> exists).
  installAPI(function () { var b = bodyOf(); return b ? [b] : []; }, function (target) {
    var s = size(), cx = target.doc.x - scrollX, cy = target.doc.y - scrollY;
    post({type: 'click', x: target.doc.x / s.w, y: target.doc.y / s.h, cx: cx, cy: cy, target: target});
  }, function () { return comment; });
  try { Object.freeze(window.mockreview); } catch (e) {}

  function boot() {
    anchors = surface(bodyOf(), host, {enabled: function () { return comment; },
      box: function () { var s = size(); return {left: -scrollX, top: -scrollY, width: s.w, height: s.h}; },
      pins: function () { return pins; }, draw: function () { draw(); post({type: 'positions', pins: pins.map(function (p) { return {i: p.i, x: p.x, y: p.y, detached: !!p.detached, doc: p.target ? p.target.doc : null}; })}); } });
    if (window.ResizeObserver) {
      sizeObserver = new ResizeObserver(changed);
      sizeObserver.__seen = new Set();
      // A canvas follows its page even when only a child's overflow grows or shrinks.
      [document.documentElement, bodyOf()].concat(bodyOf() ? Array.prototype.slice.call(bodyOf().children, 0, 40) : [])
        .forEach(function (n) { if (n) { sizeObserver.observe(n); sizeObserver.__seen.add(n); } });
    }
    addEventListener('load', hello);
    hello();
  }
  function changed() {
    clearTimeout(t);
    t = setTimeout(function () {
      hello();
      if (pins.length) { draw(); if (anchors) anchors.schedule(); }
      if (canvas && window.ResizeObserver) {
        [document.documentElement, bodyOf()].concat(bodyOf() ? Array.prototype.slice.call(bodyOf().children, 0, 40) : [])
          .forEach(function (n) { if (n && !sizeObserver.__seen.has(n)) { try { sizeObserver.observe(n); sizeObserver.__seen.add(n); } catch (e) {} } });
      }
    }, 120);
  }
  addEventListener('pagehide', function () { if (anchors) anchors.dispose(); clearTimeout(t); if (sizeObserver) sizeObserver.disconnect(); });
  if (bodyOf()) boot(); else addEventListener('DOMContentLoaded', boot, { once: true });
})();
