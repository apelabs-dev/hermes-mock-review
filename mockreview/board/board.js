/* mock-review board — vanilla JS, no build step, no network beyond this site.
 * Works in two modes (from state.json → submit.kind):
 *   local    → POST ./api/submit on the board server (tailnet)
 *   sitedata → insert a record into here.now Site Data (./.herenow/data/feedback)
 * Round kinds (state.json → rounds[].kind):
 *   decisions → the board's own canvas IS the review: it renders decisions.json natively (D1, D2 … with Today,
 *               options, Pick / Approve / Change / Skip / note) and the mocks INLINE (real app markup in a shadow
 *               root, the project's CSS adopted, media queries turned into container queries so Desktop/Mobile
 *               re-renders them in place). No iframes, no second page. Wears the project's design profile.
 *   prototype → one clickable page fills the canvas under the board bar (final "try it live" rounds).
 *   variants  → legacy (old reviews only): 1-4 pages in device frames.
 * Per-review settings (state.json → settings, stored at create time so a review never changes under the reviewer):
 *   devices / default_device / widths → the header width buttons; default_theme / show_theme_toggle → the theme
 *   toggle; pins_enabled / max_pins / require_note_on_change → pins and the Send gate. Old reviews have none →
 *   CFG_DEFAULT, i.e. the behaviour they were made with.
 * All user text is rendered with textContent. Mock HTML is parsed inert, stripped of scripts/handlers, and the
 * board CSP forbids inline script anyway. Prototype/legacy pages run in sandboxed iframes (opaque origin) and can
 * only talk to us via postMessage (size reports, comment taps).
 */
(function () {
  'use strict';

  var DEVICES = {
    desktop: { w: 1280, h: 800, label: 'Desktop', short: 'Desktop · 1280' },
    phone: { w: 390, h: 844, label: 'Phone', short: 'Phone · 390' }
  };
  var LIMIT = { pin: 600, note: 3000, guidelines: 5000, choice: 2000, pins: 40 };

  var app = document.getElementById('app');
  var S = {
    review: null,
    view: 1,
    device: pref('device', window.innerWidth < 700 ? 'both' : 'both'),
    tool: 'comment',
    draft: null,
    amending: false,
    sending: false,
    error: null,
    frames: [],
    full: null,
    pollTimer: null,
    watchTimer: null
  };

  // ---------- small helpers ----------
  function pref(k, d) { try { return localStorage.getItem('mr:pref:' + k) || d; } catch (e) { return d; } }
  function setPref(k, v) { try { localStorage.setItem('mr:pref:' + k, v); } catch (e) {} }
  function lsGet(k) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : null; } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }

  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
        else if (k === 'style') Object.keys(v).forEach(function (s) { if (s.slice(0, 2) === '--') el.style.setProperty(s, v[s]); else el.style[s] = v[s]; });
        else el.setAttribute(k, v === true ? '' : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(el, k); }); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }

  var ICONS = {
    pin: '<path d="M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z"/>',
    hand: '<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V12m0-1V4.5a1.5 1.5 0 0 1 3 0V12m0-6.5a1.5 1.5 0 0 1 3 0V13m0-4.5a1.5 1.5 0 0 1 3 0V15a7 7 0 0 1-7 7h-1.4a7 7 0 0 1-5.6-2.8L3.3 16a1.6 1.6 0 0 1 2.4-2.1L8 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    close: '<path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    send: '<path d="M4 12l16-8-6 16-2.5-6.5L4 12z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
    lock: '<rect x="5" y="11" width="14" height="9" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 11V8a4 4 0 0 1 8 0v3" fill="none" stroke="currentColor" stroke-width="1.8"/>',
    home: '<path d="M4 11.5 12 4.5l8 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M6.5 10v9.5h11V10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
    globe: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3.5 12h17M12 3.5c2.5 2.6 3.6 5.4 3.6 8.5s-1.1 5.9-3.6 8.5c-2.5-2.6-3.6-5.4-3.6-8.5S9.5 6.1 12 3.5z" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    sun: '<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
    auto: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 3.5v17a8.5 8.5 0 0 0 0-17z" fill="currentColor"/>',
    ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    note: '<path d="M5 4h14v12l-4 4H5z M15 20v-4h4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
    desktop: '<rect x="3" y="4.5" width="18" height="12" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M9 20h6M12 16.5V20" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M11 18.5h2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    tablet: '<rect x="4" y="3" width="16" height="18" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M11 18h2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    info: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 11v5.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="7.8" r="1.2"/>',
    alert: '<path d="M12 3.5l9.5 16.5h-19z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17.3" r="1.1"/>',
    stop: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M9 9l6 6M15 9l-6 6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>'
  };
  function icon(name, cls) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('class', 'ico' + (cls ? ' ' + cls : ''));
    s.innerHTML = ICONS[name]; // static strings only
    return s;
  }

  function fmtDate(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var a = new Uint8Array(16); crypto.getRandomValues(a);
    return Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }
  function pct(v) { return Math.round(v * 100) + '%'; }
  function letter(id) { return id.toUpperCase(); }

  // ---------- state ----------
  function R() { return S.review; }
  function roundObj(n) { var r = R().rounds; for (var i = 0; i < r.length; i++) if (r[i].n === n) return r[i]; return r[r.length - 1]; }
  function cur() { return roundObj(R().current_round); }
  function viewed() { return roundObj(S.view); }
  function key(suffix, n) { return 'mr:' + R().id + ':r' + (n || S.view) + ':' + suffix; }
  function localSent(n) { return lsGet(key('sent', n)); }

  function submittedFeedback(r) {
    if (r.feedback) return r.feedback;
    var ls = r.n === R().current_round ? localSent(r.n) : null;
    return ls ? ls.feedback : null;
  }
  function isEditable() {
    var r = viewed();
    if (R().status !== 'open' || r.n !== R().current_round) return false;
    return !submittedFeedback(r) || S.amending;
  }
  function isNew(r) { r = r || viewed(); return r.kind === 'decisions' || r.kind === 'prototype'; }
  function emptyDraft(r) {
    if (isNew(r)) return { marks: {}, pins: [], guidelines: '', idem: uuid() };
    var d = { choice: null, choice_note: '', guidelines: '', variants: {}, idem: uuid() };
    r.variants.forEach(function (v) { d.variants[v.id] = { note: '', pins: [] }; });
    return d;
  }
  function normalize(fb, r) {
    var d = emptyDraft(r);
    if (!fb) return d;
    if (isNew(r)) {
      Object.keys(fb.marks || {}).forEach(function (k) { var m = fb.marks[k] || {}; d.marks[k] = { status: m.status || null, option: m.option || null, note: m.note || '' }; });
      d.pins = (fb.pins || []).map(function (p) { return { block: p.block, option: p.option || null, w: p.w, x: p.x, y: p.y, text: p.text }; });
      d.guidelines = fb.guidelines || '';
      return d;
    }
    d.choice = fb.choice || null;
    d.choice_note = fb.choice_note || '';
    d.guidelines = fb.guidelines || '';
    r.variants.forEach(function (v) {
      var x = (fb.variants || {})[v.id] || {};
      d.variants[v.id] = { note: x.note || '', pins: (x.pins || []).map(function (p) { return { frame: p.frame, x: p.x, y: p.y, page_h: p.page_h, text: p.text }; }) };
    });
    return d;
  }
  function loadDraft() {
    var r = viewed();
    var fb = submittedFeedback(r);
    if (fb && !S.amending) { S.draft = normalize(fb, r); return; }
    var saved = lsGet(key(S.amending ? 'amend' : 'draft'));
    S.draft = saved ? normalize(saved, r) : (S.amending ? normalize(fb, r) : emptyDraft(r));
    S.draft.idem = (saved && saved.idem) || uuid();
  }
  var saveTimer = null;
  function saveDraft() {
    if (!isEditable()) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { lsSet(key(S.amending ? 'amend' : 'draft'), S.draft); }, 250);
  }
  function hasContent(d) {
    if (d.marks) return (d.guidelines || '').trim() !== '' || d.pins.length > 0 || Object.keys(d.marks).some(function (k) { var m = d.marks[k]; return m.status || m.option || (m.note || '').trim(); });
    if (d.choice || d.guidelines.trim() || d.choice_note.trim()) return true;
    return Object.keys(d.variants).some(function (k) { return d.variants[k].note.trim() || d.variants[k].pins.length; });
  }
  function payload(d) {
    if (d.marks) return { marks: d.marks, pins: d.pins.map(function (p) { return { block: p.block, option: p.option, w: p.w, x: +(+p.x).toFixed(4), y: +(+p.y).toFixed(4), text: p.text }; }), guidelines: d.guidelines };
    var out = { choice: d.choice, choice_note: d.choice_note, guidelines: d.guidelines, variants: {} };
    Object.keys(d.variants).forEach(function (k) {
      out.variants[k] = { note: d.variants[k].note, pins: d.variants[k].pins.map(function (p) {
        return { frame: p.frame, x: +p.x.toFixed(4), y: +p.y.toFixed(4), page_h: Math.round(p.page_h || 0), text: p.text };
      }) };
    });
    return out;
  }

  // ---------- boot ----------
  function applyTheme() {
    var t = R() ? boardTheme() : 'auto';
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
  }
  applyTheme();

  function fetchState() {
    return fetch('state.json', { cache: 'no-store', credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw new Error('state.json → HTTP ' + res.status);
      return res.json();
    });
  }

  function boot() {
    fetchState().then(function (st) {
      S.review = st;
      loadCfg(st);
      S.width = initialWidth();
      var want = parseInt((location.hash.match(/round-(\d+)/) || [])[1], 10);
      S.view = (want && st.rounds.some(function (r) { return r.n === want; })) ? want : st.current_round;
      document.title = st.title + ' · Mock review';
      loadDraft();
      render();
      maybePollForNotify();
      maybeWatchForRound();
    }).catch(function (e) {
      app.textContent = '';
      append(app, h('div', { class: 'boot error' },
        h('h1', { text: 'This review could not be loaded' }),
        h('p', { text: String(e.message || e) }),
        h('button', { class: 'btn', onclick: function () { location.reload(); }, text: 'Try again' })));
    });
  }

  // ---------- render ----------
  // ---- keep the reader's place across a full re-render (width / theme / tool switches) ----
  // render() rebuilds the page, and a mock is a different height at each width, so a kept pixel offset lands
  // on different content. Anchor on what is under the reading line instead (an option figure, else its section),
  // remember how far through it the line was, and put that same spot back under the line. The mocks settle over
  // a few frames (fonts, ResizeObserver fit), so re-anchor until the layout is still or the reader scrolls.
  var placeJob = null;
  function readingLine() {
    var y = 0;
    ['.dzbar', '.dnav', '.toolbar-wrap'].forEach(function (sel) {
      var e = document.querySelector(sel); if (!e) return;
      var b = e.getBoundingClientRect(); if (b.top <= 1 && b.bottom > y) y = b.bottom;
    });
    return y + 8;
  }
  function placeKeys(el) {   // finest first: an option inside a section, then the section itself
    var keys = [], sec = el.closest('[id]');
    var opt = el.closest('figure.opt[data-o]');
    if (opt && sec) keys.push({ id: sec.id, o: opt.dataset.o });
    if (sec) keys.push({ id: sec.id });
    return keys;
  }
  function findPlace(k) {
    var sec = document.getElementById(k.id); if (!sec) return null;
    if (!k.o) return sec;
    var f = sec.querySelector('figure.opt[data-o="' + String(k.o).replace(/["\\]/g, '') + '"]');
    return f && !f.hidden && f.offsetParent !== null ? f : null;
  }
  function capturePlace() {
    if (window.scrollY < 4) return null;   // at the top: nothing to keep
    var line = readingLine(), cands = app.querySelectorAll('figure.opt[data-o], [id^="dz-"], [id^="variant-"], #decision');
    var best = null;
    for (var i = 0; i < cands.length; i++) {
      var e = cands[i]; if (e.offsetParent === null) continue;
      var b = e.getBoundingClientRect();
      if (b.top <= line && b.bottom > line && (!best || b.height <= best.h)) best = { el: e, h: b.height };
    }
    if (!best) {   // the line sits in a gap: take the next thing below it
      for (var j = 0; j < cands.length; j++) {
        var bb = cands[j].getBoundingClientRect();
        if (cands[j].offsetParent !== null && bb.top > line) { best = { el: cands[j], h: bb.height }; break; }
      }
    }
    if (!best) return null;
    var line2 = line, out = [];
    (best.el.id ? [{ id: best.el.id }] : placeKeys(best.el)).forEach(function (k) {   // each key keeps its own spot
      var e = findPlace(k); if (!e) return;
      var r = e.getBoundingClientRect();
      out.push({ k: k, frac: r.height ? Math.max(0, Math.min(1, (line2 - r.top) / r.height)) : 0, pad: Math.max(0, r.top - line2) });
    });
    return out.length ? { keys: out } : null;
  }
  function restorePlace(pl) {
    if (!pl) return true;
    var el = null, at = null;
    for (var i = 0; i < pl.keys.length && !el; i++) {
      var k = pl.keys[i].k;
      el = findPlace(k);
      if (!el && k.o && !pl.shown) {   // the option was hidden by the one-at-a-time phone layout: show it, then anchor on it
        var sec = document.getElementById(k.id);
        if (sec && sec.querySelector('.oseg .seg-btn[data-o="' + String(k.o).replace(/["\\]/g, '') + '"]')) {
          pl.shown = true; showOpt(String(k.id).replace(/^dz-/, ''), k.o); el = findPlace(k);
        }
      }
      if (el) at = pl.keys[i];
    }
    if (!el) return false;
    var r = el.getBoundingClientRect(), line = readingLine();
    var spot = at.pad ? r.top - at.pad : r.top + at.frac * r.height;
    var d = Math.round(spot - line);
    if (Math.abs(d) > 1) window.scrollTo({ top: window.scrollY + d, behavior: 'instant' });
    return true;
  }
  function keepPlace(fn) {
    if (placeJob) placeJob.stop();
    var pl = capturePlace();
    var h0 = document.documentElement.scrollHeight;
    app.style.minHeight = h0 + 'px';   // no collapse (and no clamped scroll) while the page is rebuilt
    fn();
    if (!pl) { app.style.minHeight = ''; return; }
    var t0 = performance.now(), quiet = 0, lastH = -1, stopped = false, found = false;
    function stop() {
      if (stopped) return; stopped = true; placeJob = null; app.style.minHeight = '';
      ['wheel', 'touchstart', 'keydown', 'pointerdown'].forEach(function (ev) { window.removeEventListener(ev, stop, true); });
    }
    ['wheel', 'touchstart', 'keydown', 'pointerdown'].forEach(function (ev) { window.addEventListener(ev, stop, true); });
    placeJob = { stop: stop };
    (function tick() {
      if (stopped) return;
      if (found) app.style.minHeight = '';   // content is in: let the page take its real height
      found = restorePlace(pl) || found;
      var h = document.documentElement.scrollHeight;
      quiet = (found && h === lastH) ? quiet + 1 : 0; lastH = h;
      if (quiet >= 12 || performance.now() - t0 > 2500) { restorePlace(pl); stop(); return; }
      requestAnimationFrame(tick);
    })();
  }

  function render() {
    closePopover();
    S.frames = [];
    app.textContent = '';
    document.body.classList.toggle('tool-comment', S.tool === 'comment');
    document.body.classList.toggle('tool-interact', S.tool === 'interact');
    document.body.classList.toggle('is-readonly', !isEditable());
    var r = viewed();
    applyDesign(r);
    document.body.classList.toggle('dz-mode', isNew(r));
    if (isNew(r)) { renderNew(r); return; }
    append(app, [renderHeader(), renderToolbar(), renderStatusBanner(), renderVariants(r), renderDecision(r), renderSendbar()]);
    requestAnimationFrame(layoutAll);
  }

  function renderHeader() {
    var st = R();
    var modeBadge = st.mode === 'herenow'
      ? h('span', { class: 'badge badge-public', title: 'Hosted on here.now' }, icon('globe'), 'here.now')
      : st.mode === 'local'
        ? h('span', { class: 'badge badge-private', title: 'Served from this computer at its local address, no Tailscale' }, icon('home'), 'Local')
        : h('span', { class: 'badge badge-private', title: 'Served from the board server, reachable on the tailnet only' }, icon('lock'), 'Tailnet only');
    var themeBtn = themeControl({});
    var brief = st.brief ? h('p', { class: 'brief', text: st.brief }) : null;
    if (brief) {
      brief.addEventListener('click', function () { brief.classList.toggle('open'); });
      brief.title = 'Tap to expand';
    }
    return h('header', { class: 'top' },
      h('div', { class: 'top-row' },
        h('div', { class: 'brand' }, h('span', { class: 'brand-mark' }, icon('pin')), h('span', { class: 'brand-name', text: 'Mock review' })),
        h('div', { class: 'top-actions' }, modeBadge, themeBtn)),
      h('h1', { class: 'title', text: st.title }),
      brief);
  }

  function seg(options, value, onpick, label) {
    return h('div', { class: 'seg', role: 'radiogroup', 'aria-label': label },
      options.map(function (o) {
        return h('button', {
          class: 'seg-btn' + (o.value === value ? ' on' : ''), role: 'radio', 'aria-checked': o.value === value ? 'true' : 'false',
          title: o.title || null, onclick: function () { onpick(o.value); }
        }, o.icon ? icon(o.icon) : null, h('span', { text: o.label }));
      }));
  }

  function renderToolbar() {
    var st = R();
    var rounds = h('nav', { class: 'rounds', 'aria-label': 'Rounds' },
      st.rounds.map(function (r) {
        var sent = !!submittedFeedback(r);
        return h('button', {
          class: 'round-tab' + (r.n === S.view ? ' on' : ''),
          onclick: function () { switchRound(r.n); }
        }, h('span', { text: 'Round ' + r.n }),
          r.n === st.current_round && st.status === 'open' ? h('span', { class: 'dot-live', title: 'Current round' }) : null,
          sent ? h('span', { class: 'tick', title: 'Feedback sent' }, icon('check')) : null);
      }));
    var devices = seg([
      { value: 'both', label: 'Both' }, { value: 'desktop', label: 'Desktop' }, { value: 'phone', label: 'Phone' }
    ], S.device, function (v) { S.device = v; setPref('device', v); keepPlace(render); }, 'Frames');
    var tools = isEditable() ? seg([
      { value: 'comment', label: 'Comment', icon: 'pin', title: 'Tap a mock to drop a pin' },
      { value: 'interact', label: 'Interact', icon: 'hand', title: 'Click through the mocks' }
    ], S.tool, setTool, 'Tool') : null;
    var r = viewed();
    var jumps = h('div', { class: 'jumps' }, r.variants.map(function (v) {
      return h('a', { class: 'jump', href: '#variant-' + v.id, text: letter(v.id),
        onclick: function (e) { e.preventDefault(); var t = document.getElementById('variant-' + v.id); if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
    }));
    var hint = isEditable() && S.tool === 'comment'
      ? h('p', { class: 'hint' }, icon('pin'), h('span', { class: 'hint-long', text: 'Tap anywhere on a mock to pin a comment. Switch to Interact to click through it.' }), h('span', { class: 'hint-short', text: 'Tap a mock to pin a comment.' }))
      : null;
    return h('div', { class: 'toolbar-wrap', id: 'toolbar' },
      h('div', { class: 'toolbar' }, rounds, h('div', { class: 'tb-right' }, devices, tools)),
      h('div', { class: 'toolbar-sub' }, hint, jumps));
  }

  function renderStatusBanner() {
    var st = R(), r = viewed();
    if (st.status !== 'open') {
      return h('div', { class: 'banner banner-muted' }, 'This review is closed. Everything below is read-only.');
    }
    if (r.n !== st.current_round) {
      var fb = submittedFeedback(r);
      return h('div', { class: 'banner banner-muted' },
        h('span', { text: 'Round ' + r.n + ' · read-only' + (r.submission ? ' · feedback sent ' + fmtDate(r.submission.submitted_at) : fb ? '' : ' · no feedback was sent') }),
        h('button', { class: 'link-btn', onclick: function () { switchRound(st.current_round); },
          text: r.submission ? 'Round ' + st.current_round + ' is ready →' : 'Go to round ' + st.current_round + ' →' }));
    }
    if (S.amending) {
      return h('div', { class: 'banner banner-warn' },
        h('span', { text: 'Amending round ' + r.n + ' — sending replaces the feedback Hermes already has.' }),
        h('button', { class: 'link-btn', onclick: cancelAmend, text: 'Cancel amend' }));
    }
    if (submittedFeedback(r)) {
      var pend = r.submission && r.submission.status === 'pending_notify';
      var stalled = r.submission && r.submission.stalled_notice_at;
      var cls = stalled ? 'banner-warn' : 'banner-ok';
      return h('div', { class: 'banner ' + cls, role: 'status' },
        h('span', { text: stalled
          ? 'Hermes may have stalled — ask in the chat or resend from the board.'
          : pend
            ? 'Feedback saved ✓ — still reaching Hermes. It will reply soon.'
            : 'Feedback sent ✓ — Hermes was notified and is working on it.' }));
    }
    return null;
  }

  function renderVariants(r) {
    var fb = isEditable() ? null : submittedFeedback(r);
    var grid = h('main', { class: 'variants', style: { '--n': String(r.variants.length) } });
    r.variants.forEach(function (v) { grid.appendChild(renderVariant(r, v, fb)); });
    return grid;
  }

  function devicesShown() {
    return S.device === 'both' ? ['desktop', 'phone'] : [S.device];
  }

  function renderVariant(r, v, fb) {
    var editable = isEditable();
    var picked = S.draft.choice === v.id;
    var pickBtn = editable
      ? h('button', {
          class: 'btn btn-pick' + (picked ? ' on' : ''), 'aria-pressed': picked ? 'true' : 'false',
          onclick: function () { setChoice(picked ? null : v.id); }
        }, picked ? icon('check') : null, picked ? 'Picked' : 'Pick ' + letter(v.id))
      : (picked ? h('span', { class: 'chip chip-picked' }, icon('check'), 'Picked') : null);
    var head = h('header', { class: 'v-head' },
      h('span', { class: 'v-letter', text: letter(v.id) }),
      h('div', { class: 'v-titles' }, h('h2', { class: 'v-label', text: v.label }), v.stance ? h('p', { class: 'v-stance', text: v.stance }) : null),
      h('div', { class: 'v-actions' },
        h('button', { class: 'icon-btn', title: 'Open full size', 'aria-label': 'Open variant ' + letter(v.id) + ' full size', onclick: function () { openFull(v.id, devicesShown()[0]); } }, icon('expand')),
        pickBtn));
    var frames = h('div', { class: 'v-frames v-frames-' + S.device });
    devicesShown().forEach(function (d) { frames.appendChild(makeFrame(r, v, d, 'card').el); });
    var art = h('article', { class: 'variant' + (picked ? ' is-picked' : ''), id: 'variant-' + v.id, 'data-variant': v.id },
      head, frames, renderVariantFeedback(r, v));
    return art;
  }

  function renderVariantFeedback(r, v) {
    var editable = isEditable();
    var vd = S.draft.variants[v.id];
    var box = h('div', { class: 'v-feedback' });
    var list = h('ol', { class: 'pin-list', id: 'pins-' + v.id });
    box.appendChild(list);
    fillPinList(list, v.id);
    if (editable) {
      var ta = h('textarea', {
        class: 'field', rows: '2', maxlength: String(LIMIT.note), placeholder: 'General note on ' + letter(v.id) + ' (optional)',
        'aria-label': 'General note on variant ' + letter(v.id)
      });
      ta.value = vd.note;
      ta.addEventListener('input', function () { vd.note = ta.value; saveDraft(); updateSendbar(); autoGrow(ta); });
      requestAnimationFrame(function () { autoGrow(ta); });
      box.appendChild(ta);
    } else if (vd.note) {
      box.appendChild(h('div', { class: 'note-ro' }, h('span', { class: 'ro-label' }, icon('note'), 'Note'), h('p', { text: vd.note })));
    }
    return box;
  }

  function fillPinList(list, vid) {
    list.textContent = '';
    var pins = S.draft.variants[vid].pins;
    if (!pins.length) {
      list.appendChild(h('li', { class: 'pin-empty', text: isEditable() ? 'No pins yet — tap the mock to add one.' : 'No pins.' }));
      return;
    }
    pins.forEach(function (p, i) {
      list.appendChild(h('li', { class: 'pin-item' },
        h('button', { class: 'pin-row', onclick: function () { focusPin(vid, i); } },
          h('span', { class: 'pin-num', text: String(i + 1) }),
          h('span', { class: 'pin-body' },
            h('span', { class: 'pin-where', text: DEVICES[p.frame].label + ' · ' + pct(p.x) + ', ' + pct(p.y) }),
            h('span', { class: 'pin-text', text: p.text })))));
    });
  }

  function autoGrow(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 2, 320) + 'px'; }

  function renderDecision(r) {
    var editable = isEditable();
    var d = S.draft;
    var sec = h('section', { class: 'decision', id: 'decision' });
    sec.appendChild(h('div', { class: 'd-head' },
      h('h2', { text: editable ? 'Decision' : 'Decision · round ' + r.n }),
      editable ? h('p', { class: 'muted', text: 'Pick a direction, then tell Hermes what the next round should do.' }) : null));
    var opts = r.variants.map(function (v) { return { id: v.id, title: letter(v.id) + ' · ' + v.label }; })
      .concat([{ id: 'none', title: 'None / combine' }]);
    var choices = h('div', { class: 'choices', role: 'radiogroup', 'aria-label': 'Pick a variant' },
      opts.map(function (o) {
        var on = d.choice === o.id;
        return h('button', {
          class: 'choice' + (on ? ' on' : '') + (o.id === 'none' ? ' choice-none' : ''), role: 'radio', 'aria-checked': on ? 'true' : 'false',
          disabled: editable ? null : true, onclick: function () { setChoice(on ? null : o.id); }
        }, h('span', { class: 'radio' }, on ? icon('check') : null), h('span', { text: o.title }));
      }));
    sec.appendChild(choices);
    sec.appendChild(fieldBlock('Why this one — or what to combine', 'choice_note', LIMIT.choice, 2,
      d.choice === 'none' ? 'e.g. layout from A, colours and type from C' : 'Optional: what made it win, what to keep'));
    sec.appendChild(fieldBlock('Guidelines for the next round', 'guidelines', LIMIT.guidelines, 4,
      'Direction for Hermes: what to change, keep, explore next…'));
    return sec;
  }

  function fieldBlock(label, prop, max, rows, placeholder) {
    var editable = isEditable();
    var id = 'f-' + prop;
    if (!editable) {
      return h('div', { class: 'field-ro' }, h('span', { class: 'ro-label', text: label }),
        S.draft[prop] ? h('p', { text: S.draft[prop] }) : h('p', { class: 'muted', text: '—' }));
    }
    var ta = h('textarea', { class: 'field', id: id, rows: String(rows), maxlength: String(max), placeholder: placeholder });
    ta.value = S.draft[prop];
    var count = h('span', { class: 'count', text: '' });
    function upd() { count.textContent = ta.value.length > max * 0.8 ? ta.value.length + ' / ' + max : ''; }
    ta.addEventListener('input', function () { S.draft[prop] = ta.value; saveDraft(); updateSendbar(); upd(); autoGrow(ta); });
    upd();
    requestAnimationFrame(function () { autoGrow(ta); });
    return h('label', { class: 'field-block', for: id }, h('span', { class: 'field-label' }, label, count), ta);
  }

  function setChoice(c) {
    S.draft.choice = c;
    saveDraft();
    document.querySelectorAll('.variant').forEach(function (a) {
      var vid = a.getAttribute('data-variant'), on = c === vid;
      a.classList.toggle('is-picked', on);
      var b = a.querySelector('.btn-pick');
      if (b) {
        b.classList.toggle('on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        b.textContent = '';
        if (on) b.appendChild(icon('check'));
        b.appendChild(document.createTextNode(on ? 'Picked' : 'Pick ' + letter(vid)));
      }
    });
    var dec = document.getElementById('decision');
    if (dec) dec.replaceWith(renderDecision(viewed()));
    updateSendbar();
  }

  // ---------- frames ----------
  // Each frame is a real device viewport (1280×800 / 390×844) scaled to fit.
  // The mock scrolls natively; the injected probe reports taps and draws pins.
  function makeFrame(r, v, device, ctx) {
    var D = DEVICES[device];
    var f = { r: r, vid: v.id, device: device, ctx: ctx, W: D.w, H: D.h, pageH: D.h, ghost: null, alive: false };
    f.iframe = h('iframe', {
      src: v.file, title: 'Variant ' + letter(v.id) + ' — ' + v.label + ' (' + D.label + ')',
      sandbox: 'allow-scripts', referrerpolicy: 'no-referrer'
    });
    f.iframe.addEventListener('load', function () { pushState(f); });
    f.scaler = h('div', { class: 'scaler' }, f.iframe);
    f.viewport = h('div', { class: 'viewport' }, f.scaler);
    var chrome = device === 'desktop'
      ? h('div', { class: 'chrome-bar' }, h('i'), h('i'), h('i'), h('span', { text: D.short }))
      : h('div', { class: 'chrome-cap', text: D.short });
    f.el = h('figure', { class: 'frame frame-' + device + ' frame-' + ctx }, device === 'desktop' ? null : chrome,
      h('div', { class: 'bezel' }, device === 'desktop' ? chrome : null, f.viewport));
    S.frames.push(f);
    if (window.ResizeObserver) new ResizeObserver(function () { layout(f); }).observe(f.el);
    return f;
  }

  function layout(f) {
    if (f.ctx === 'proto') return;
    var chromeW = f.device === 'phone' ? 12 : 2;
    var avail = f.el.clientWidth - chromeW;
    if (avail <= 0) return;
    var s, vh;
    if (f.ctx === 'full') {
      var bodyW = f.el.parentNode.clientWidth - 36 - chromeW;
      s = bodyW < f.W && bodyW > f.W * 0.85 ? bodyW / f.W : 1;
      var room = (f.el.parentNode.clientHeight - 36 - (f.device === 'phone' ? 30 : 24)) / s;
      vh = Math.max(320, Math.min(f.device === 'phone' ? f.H : 4000, room));
    } else {
      s = Math.min(1, avail / f.W);
      vh = f.H;
    }
    f.scale = s;
    f.viewport.style.width = Math.round(f.W * s) + 'px';
    f.viewport.style.height = Math.round(vh * s) + 'px';
    f.scaler.style.width = f.W + 'px';
    f.scaler.style.height = vh + 'px';
    f.scaler.style.transform = 'scale(' + s + ')';
    f.iframe.style.width = f.W + 'px';
    f.iframe.style.height = vh + 'px';
    if (f.alive) pushState(f);
  }
  function layoutAll() { S.frames.forEach(layout); }

  function framePins(f) {
    var out = [];
    S.draft.variants[f.vid].pins.forEach(function (p, i) {
      if (p.frame === f.device) out.push({ i: i, n: i + 1, x: p.x, y: p.y });
    });
    if (f.ghost) out.push({ i: -1, n: S.draft.variants[f.vid].pins.length + 1, x: f.ghost.x, y: f.ghost.y, ghost: true });
    return out;
  }
  function pushState(f) {
    try {
      if (f.ctx === 'proto') return pushProto(f);
      f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'state', comment: isEditable() && S.tool === 'comment', scale: f.scale || 1, pins: framePins(f) }, '*');
    } catch (e) {}
  }
  function pushAll(vid) { S.frames.forEach(function (f) { if (!vid || f.vid === vid) pushState(f); }); }

  function frameFor(win) {
    for (var i = 0; i < S.frames.length; i++) if (S.frames[i].iframe.contentWindow === win) return S.frames[i];
    return null;
  }
  function anchorOf(f, cx, cy) {
    var r = f.iframe.getBoundingClientRect();
    return { x: r.left + num(cx, 0, f.W) * f.scale, y: r.top + num(cy, 0, 4000) * f.scale };
  }
  function num(v, lo, hi) { v = +v; return isFinite(v) ? Math.min(Math.max(v, lo), hi) : lo; }

  window.addEventListener('message', function (ev) {
    var d = ev.data;
    if (!d || d.__mockreview !== 1 || typeof d.type !== 'string') return;
    var f = frameFor(ev.source);
    if (!f) return;
    if (f.ctx === 'proto') return protoMessage(f, d);
    if (d.type === 'hello') {
      f.alive = true;
      f.pageH = Math.round(num(d.h, 100, 20000));
      pushState(f);
      if (f.pendingFocus !== undefined) {
        var fi = f.pendingFocus; f.pendingFocus = undefined;
        setTimeout(function () { try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'focus', i: fi }, '*'); } catch (e) {} }, 60);
      }
    } else if (d.type === 'click') {
      if (!isEditable() || S.tool !== 'comment') return;
      f.pageH = Math.round(num(d.h, 100, 20000));
      openPinEditor({ vid: f.vid, frame: f.device, x: num(d.x, 0, 1), y: num(d.y, 0, 1), page_h: f.pageH, index: -1, anchor: anchorOf(f, d.cx, d.cy), f: f });
    } else if (d.type === 'pin') {
      var i = num(d.i, -1, 99) | 0;
      var p = S.draft.variants[f.vid].pins[i];
      if (!p || p.frame !== f.device) return;
      openPinEditor({ vid: f.vid, frame: p.frame, x: p.x, y: p.y, page_h: p.page_h, index: i, anchor: anchorOf(f, d.cx, d.cy), f: f });
    }
  });

  function redrawVariantPins(vid) {
    pushAll(vid);
    var list = document.getElementById('pins-' + vid);
    if (list) fillPinList(list, vid);
    updateSendbar();
  }

  function focusPin(vid, i) {
    var p = S.draft.variants[vid].pins[i];
    var ctx = S.full ? 'full' : 'card';
    var f = S.frames.filter(function (x) { return x.vid === vid && x.device === p.frame && x.ctx === ctx; })[0];
    if (!f) { if (!S.full) openFull(vid, p.frame, i); return; }
    f.el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'focus', i: i }, '*'); } catch (e) {}
  }

  // ---------- pin popover ----------
  var pop = null;
  function closePopover() {
    if (!pop) return;
    pop.el.remove();
    if (pop.onClose) { var oc = pop.onClose; pop.onClose = null; oc(); }
    if (pop.f && pop.f.ghost) { pop.f.ghost = null; pushState(pop.f); }
    pop = null;
  }
  function openPinEditor(o) {
    closePopover();
    var editable = isEditable();
    var isNew = o.index < 0;
    var pins = S.draft.variants[o.vid].pins;
    if (isNew && pins.length >= LIMIT.pins) { toast('That’s ' + LIMIT.pins + ' pins on this variant — use the note for the rest.', 'warn'); return; }
    if (isNew && o.f) { o.f.ghost = { x: o.x, y: o.y }; pushState(o.f); }
    var title = (isNew ? 'New pin' : 'Pin ' + (o.index + 1)) + ' · ' + letter(o.vid) + ' · ' + DEVICES[o.frame].label;
    var where = '~' + pct(o.x) + ', ' + pct(o.y) + ' of the page';
    var body;
    var ta = null;
    if (editable) {
      ta = h('textarea', { class: 'field', rows: '3', maxlength: String(LIMIT.pin), placeholder: 'What should change here?', 'aria-label': 'Pin comment' });
      ta.value = isNew ? '' : pins[o.index].text;
      body = ta;
    } else {
      body = h('p', { class: 'pop-text', text: pins[o.index] ? pins[o.index].text : '' });
    }
    function save() {
      var text = ta.value.replace(/\s+/g, ' ').trim();
      if (!text) { ta.focus(); ta.classList.add('shake'); setTimeout(function () { ta.classList.remove('shake'); }, 400); return; }
      if (isNew) pins.push({ frame: o.frame, x: o.x, y: o.y, page_h: Math.round(o.page_h), text: text.slice(0, LIMIT.pin) });
      else pins[o.index].text = text.slice(0, LIMIT.pin);
      saveDraft(); closePopover(); redrawVariantPins(o.vid);
    }
    function del() { pins.splice(o.index, 1); saveDraft(); closePopover(); redrawVariantPins(o.vid); }
    var actions = editable
      ? h('div', { class: 'pop-actions' },
          !isNew ? h('button', { class: 'btn btn-ghost btn-danger', onclick: del, text: 'Delete' }) : null,
          h('span', { class: 'spacer' }),
          h('button', { class: 'btn btn-ghost', onclick: closePopover, text: 'Cancel' }),
          h('button', { class: 'btn btn-primary', onclick: save, text: isNew ? 'Add pin' : 'Save' }))
      : h('div', { class: 'pop-actions' }, h('span', { class: 'spacer' }), h('button', { class: 'btn btn-ghost', onclick: closePopover, text: 'Close' }));
    var el = h('div', { class: 'popover', role: 'dialog', 'aria-label': title },
      h('div', { class: 'pop-head' }, h('span', { class: 'pin pin-static' }, h('span', { text: String(isNew ? pins.length + 1 : o.index + 1) })),
        h('div', null, h('strong', { text: title }), h('span', { class: 'pop-where', text: where }))),
      body, actions);
    if (ta) ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey || !e.shiftKey)) { e.preventDefault(); save(); }
    });
    document.body.appendChild(el);
    pop = { el: el, f: isNew ? o.f : null };
    var sheet = window.innerWidth < 640;
    el.classList.toggle('sheet', sheet);
    if (!sheet) {
      var w = el.offsetWidth, hh = el.offsetHeight;
      var left = o.anchor.x + 18;
      if (left + w > window.innerWidth - 12) left = o.anchor.x - w - 18;
      left = Math.max(12, left);
      var top = Math.min(Math.max(12, o.anchor.y - 28), window.innerHeight - hh - 12);
      el.style.left = left + 'px';
      el.style.top = top + 'px';
    }
    if (ta) setTimeout(function () { ta.focus({ preventScroll: true }); }, 30);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (pop) closePopover(); else if (S.full) closeFull();
  });
  document.addEventListener('pointerdown', function (e) {
    if (pop && !pop.el.contains(e.target) && !(e.target.closest && e.target.closest('.pin-row'))) closePopover();
  });
  window.addEventListener('resize', function () { if (pop && !pop.el.classList.contains('sheet')) closePopover(); });

  // ---------- full size ----------
  function openFull(vid, device, focusIndex) {
    closePopover();
    var r = viewed();
    var v = r.variants.filter(function (x) { return x.id === vid; })[0];
    if (S.full) S.full.el.remove();
    S.frames = S.frames.filter(function (f) { return f.ctx !== 'full'; });
    var f = makeFrame(r, v, device, 'full');
    if (focusIndex !== undefined) f.pendingFocus = focusIndex;
    var tools = isEditable() ? seg([
      { value: 'comment', label: 'Comment', icon: 'pin' }, { value: 'interact', label: 'Interact', icon: 'hand' }
    ], S.tool, function (t) { setTool(t); openFull(vid, device); }, 'Tool') : null;
    var el = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Variant ' + letter(vid) + ' full size' },
      h('div', { class: 'modal-bar' },
        h('div', { class: 'modal-title' }, h('span', { class: 'v-letter', text: letter(vid) }), h('strong', { text: v.label })),
        h('div', { class: 'modal-tools' },
          seg([{ value: 'desktop', label: 'Desktop' }, { value: 'phone', label: 'Phone' }], device, function (d) { openFull(vid, d); }, 'Device'),
          tools,
          h('a', { class: 'icon-btn', href: v.file, target: '_blank', rel: 'noopener noreferrer', title: 'Open in a new tab', 'aria-label': 'Open in a new tab' }, icon('ext')),
          h('button', { class: 'icon-btn', onclick: closeFull, title: 'Close (Esc)', 'aria-label': 'Close' }, icon('close')))),
      h('div', { class: 'modal-body modal-' + device }, f.el));
    document.body.appendChild(el);
    document.body.classList.add('modal-open');
    S.full = { el: el, vid: vid, device: device };
    requestAnimationFrame(function () { layout(f); });
  }
  function closeFull() {
    closePopover();
    if (!S.full) return;
    S.full.el.remove();
    S.full = null;
    S.frames = S.frames.filter(function (f) { return f.ctx !== 'full'; });
    document.body.classList.remove('modal-open');
  }


  // ======================================================================================================
  // Decision + prototype rounds: the board's own canvas IS the review (no iframe around the decisions).
  // ======================================================================================================
  var ASSETS = {};          // round n -> Promise<{dec, sheetText}>
  var MOCKS = [];           // live inline mocks on screen
  var renderSeq = 0;
  S.width = window.innerWidth < 760 ? 'phone' : 'desktop';   // Desktop / Mobile: defaults to the device in hand
  S.show = {};              // decision id -> option shown behind the A|B|C toggle
  var DESIGN_VARS = ['--bg', '--surface', '--surface-2', '--line', '--line-strong', '--text', '--muted', '--faint', '--accent',
    '--accent-hover', '--accent-ink', '--accent-soft', '--ok', '--ok-soft', '--warn', '--warn-soft', '--err', '--err-soft', '--font', '--bar', '--pin-ring'];

  function roundBase(r) { return 'rounds/' + r.n + '/a/'; }
  function narrow() { return window.innerWidth < 760; }
  function mark(id) { return (S.draft.marks && S.draft.marks[id]) || { status: null, option: null, note: '' }; }
  function setMark(id, patch) {
    var m = Object.assign({}, mark(id), patch);
    if (!m.status && !m.option && !(m.note || '').trim()) delete S.draft.marks[id]; else S.draft.marks[id] = m;
    saveDraft(); updateSendbar(); refreshBlock(id); updateTally();
  }
  function initials(label) {
    var w = String(label || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/);
    return ((w[0] || '?')[0] + (w.length > 1 ? w[1][0] : (w[0][1] || ''))).toUpperCase();
  }

  // ---- per-review settings (state.json → settings) ----
  var CFG_DEFAULT = { devices: ['desktop', 'mobile'], default_device: 'auto', default_theme: 'project', show_theme_toggle: true,
    mobile_width: 390, tablet_width: 820, pins_enabled: true, require_note_on_change: false, max_pins: 40 };
  var CFG = Object.assign({}, CFG_DEFAULT);
  var WKEY = { desktop: 'desktop', tablet: 'tablet', mobile: 'phone' };   // setting name → width key (pins keep 'phone')
  var WLABEL = { desktop: 'Desktop', tablet: 'Tablet', phone: 'Mobile' };
  var WICON = { desktop: 'desktop', tablet: 'tablet', phone: 'phone' };
  var WORDER = ['phone', 'tablet', 'desktop'];
  function loadCfg(st) {
    CFG = Object.assign({}, CFG_DEFAULT, st.settings || {});
    var ok = (CFG.devices || []).filter(function (d) { return WKEY[d]; });
    CFG.devices = ok.length ? ok : CFG_DEFAULT.devices.slice();
    LIMIT.pins = Math.max(1, +CFG.max_pins || CFG_DEFAULT.max_pins);
    document.body.classList.toggle('no-pins', !CFG.pins_enabled);
  }
  function widths() { return CFG.devices.map(function (d) { return WKEY[d]; }); }
  function viewerWidth() {
    var w = window.innerWidth, coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    return w < 760 ? 'phone' : (w < 1100 && coarse) ? 'tablet' : 'desktop';
  }
  function initialWidth() {   // default_device, or the viewer's device; clamped to the enabled set (nearest size)
    var ws = widths(), want = CFG.default_device === 'auto' ? viewerWidth() : (WKEY[CFG.default_device] || viewerWidth());
    if (ws.indexOf(want) >= 0) return want;
    var best = ws[0], bd = 9;
    ws.forEach(function (w) { var dd = Math.abs(WORDER.indexOf(w) - WORDER.indexOf(want)); if (dd < bd) { bd = dd; best = w; } });
    return best;
  }
  function wlabel(w) { return WLABEL[w] || 'Desktop'; }
  function mockWidth(b) {
    var dw = b.desktop_width || 1280;
    return S.width === 'phone' ? CFG.mobile_width : S.width === 'tablet' ? Math.min(CFG.tablet_width, dw) : dw;
  }
  function pinsOn() { return !!CFG.pins_enabled; }
  function missingNotes() {   // require_note_on_change: decisions marked Change with no note yet
    var r = viewed();
    if (!CFG.require_note_on_change || !isNew(r) || !S.draft || !S.draft.marks || !isEditable()) return [];
    return (r.decisions || []).filter(function (d) { var m = S.draft.marks[d.id]; return m && m.status === 'change' && !(m.note || '').trim(); });
  }

  // ---- theme: starts at default_theme; the toggle (if shown) remembers the viewer's flip per review ----
  function boardTheme() {   // no design profile: the board's own auto / light / dark
    var init = CFG.default_theme === 'dark' || CFG.default_theme === 'light' ? CFG.default_theme : 'auto';
    return CFG.show_theme_toggle ? pref('theme:' + R().id, init) : init;
  }

  // ---- design profile: the project's colours + font become the board's own tokens ----
  function designTheme(r) {
    var d = r && r.design; if (!d) return null;
    var want = CFG.default_theme !== 'project' && d.themes[CFG.default_theme] ? CFG.default_theme : d.default_theme;
    var t = CFG.show_theme_toggle ? pref('dtheme:' + R().id + ':' + d.name, want) : want;
    return d.themes[t] ? t : want;
  }
  function applyDesign(r) {
    var root = document.documentElement, d = r && isNew(r) ? r.design : null;
    DESIGN_VARS.forEach(function (v) { root.style.removeProperty(v); });
    var fav = document.querySelector('link[rel=icon]');
    if (!d) {
      applyTheme();
      if (fav) fav.href = 'favicon.svg';
      return;
    }
    var t = designTheme(r), k = d.themes[t];
    root.setAttribute('data-theme', t);
    function set(n, v) { if (v) root.style.setProperty(n, v); }
    function soft(c, pct) { return 'color-mix(in srgb, ' + c + ' ' + pct + '%, transparent)'; }
    set('--bg', k.bg); set('--surface', k.surface); set('--surface-2', k.surface2 || k.surface);
    set('--line', k.line || soft(k.text, 14)); set('--line-strong', k.line2 || k.line || soft(k.text, 22));
    set('--text', k.text); set('--muted', k.muted || soft(k.text, 72)); set('--faint', k.faint || k.muted || soft(k.text, 55));
    set('--accent', k.accent); set('--accent-hover', k.accent); set('--accent-ink', k.accentInk || k.bg);
    set('--accent-soft', soft(k.accent, 16)); set('--bar', k.surface2 || k.surface);
    if (k.ok) { set('--ok', k.ok); set('--ok-soft', soft(k.ok, 14)); }
    if (k.warn) { set('--warn', k.warn); set('--warn-soft', soft(k.warn, 15)); }
    if (k.err) { set('--err', k.err); set('--err-soft', soft(k.err, 13)); }
    if (d.font) set('--font', d.font);
    root.style.colorScheme = t;
    if (d.fonts_css) {
      var href = roundBase(r) + 'design/' + d.fonts_css, l = document.getElementById('mr-fonts');
      if (!l) { l = h('link', { id: 'mr-fonts', rel: 'stylesheet' }); document.head.appendChild(l); }
      if (l.getAttribute('href') !== href) l.setAttribute('href', href);
    }
    if (fav) fav.href = d.icon ? roundBase(r) + 'design/' + d.icon : 'favicon.svg';
  }

  // The app's CSS is written for a viewport; inside the board a mock is a box. Width-only media queries become
  // container queries on that box, so "Mobile" (a 390 px box) gets the app's real phone layout.
  function toContainer(css) {
    return css.replace(/@media\s+([^{;]+)\{/g, function (m, cond) {
      var c = cond.trim().replace(/^(only\s+)?screen\s+and\s+/i, '');
      return /^(\(\s*(min|max)-width\s*:\s*[\d.]+px\s*\)\s*(and\s*)?)+$/i.test(c) ? '@container mock ' + c + ' {' : m;
    });
  }
  var MOCK_BASE = ':host{display:block}.cq{container:mock/inline-size;pointer-events:none}' +
    '.cq,.cq *{-webkit-user-select:none;user-select:none}';
  function loadAssets(r) {
    if (ASSETS[r.n]) return ASSETS[r.n];
    var base = roundBase(r), d = r.design, out = { dec: null, css: '' }, jobs = [];
    function get(url, as) {
      return fetch(url, { cache: 'no-store', credentials: 'same-origin' }).then(function (x) {
        if (!x.ok) throw new Error(url.split('/').pop() + ' → HTTP ' + x.status);
        return as === 'json' ? x.json() : x.text();
      });
    }
    if (r.kind === 'decisions') jobs.push(get(base + 'decisions.json', 'json').then(function (j) { out.dec = j; }));
    if (d && d.mock_css) jobs.push(get(base + 'design/' + d.mock_css, 'text').then(function (t) { out.css = t; }));
    ASSETS[r.n] = Promise.all(jobs).then(function () {
      out.css = MOCK_BASE + toContainer(out.css + '\n' + ((out.dec && out.dec.extra_css) || ''));
      try { out.sheet = new CSSStyleSheet(); out.sheet.replaceSync(out.css); } catch (e) { out.sheet = null; }
      return out;
    });
    ASSETS[r.n].catch(function () { delete ASSETS[r.n]; });
    return ASSETS[r.n];
  }

  // Mock HTML is my own static markup, but treat it as untrusted anyway: parsed inert, no scripts, no handlers.
  function safeLink(value) {
    // URL() alone normalizes whitespace and relative addresses. Reject those first.
    if (typeof value !== 'string' || /[\s\u0000-\u001f\u007f-\u009f]/.test(value) || !/^https?:\/\//i.test(value)) return null;
    try {
      var url = new URL(value);
      return (url.protocol === 'https:' || url.protocol === 'http:') && url.hostname ? url.href : null;
    } catch (e) { return null; }
  }
  function secureLink(el, url) {
    el.setAttribute('href', url);
    el.setAttribute('target', '_blank');
    el.setAttribute('rel', 'noopener noreferrer');
  }
  function cleanFragment(html) {
    var t = document.createElement('template');
    t.innerHTML = html;
    t.content.querySelectorAll('script,iframe,frame,object,embed,base,meta,link,form').forEach(function (n) {
      if (n.tagName === 'FORM') { var dv = document.createElement('div'); while (n.firstChild) dv.appendChild(n.firstChild); n.replaceWith(dv); } else n.remove();
    });
    t.content.querySelectorAll('*').forEach(function (el) {
      [].slice.call(el.attributes).forEach(function (a) {
        var n = a.name.toLowerCase(), v = a.value.trim().toLowerCase();
        if (n.slice(0, 2) === 'on' || n === 'xlink:href' ||
            (n === 'href' && el.tagName.toUpperCase() !== 'A') ||
            ((n === 'href' || n === 'src' || n === 'action' || n === 'formaction') && v.slice(0, 11) === 'javascript:')) el.removeAttribute(a.name);
      });
      if (el.tagName.toUpperCase() === 'A') {
        var url = safeLink(el.getAttribute('href'));
        ['href', 'target', 'rel', 'ping', 'download', 'referrerpolicy'].forEach(function (n) { el.removeAttribute(n); });
        if (url) secureLink(el, url);
      }
    });
    return t.content;
  }

  // ---- the bar: project icon · title · rounds | Desktop/Mobile · theme · Tailnet/here.now · Send ----
  function projectIcon(r) {
    var d = r.design, label = (d && d.label) || R().title;
    var fallback = h('span', { class: 'proj-icon proj-fallback', 'aria-hidden': 'true', text: initials(label) });
    if (!d || !d.icon) return fallback;
    var img = h('img', { class: 'proj-icon', src: roundBase(r) + 'design/' + d.icon, alt: label });
    img.addEventListener('error', function () { img.replaceWith(fallback); });
    return img;
  }
  function themeControl(r) {
    if (!CFG.show_theme_toggle) return null;
    var d = r.design;
    if (d) {
      var names = Object.keys(d.themes);
      if (names.length < 2) return null;
      var t = designTheme(r), next = names[(names.indexOf(t) + 1) % names.length];
      return h('button', { class: 'icon-btn theme-btn', title: d.label + ' ' + t + ' theme · switch to ' + next, 'aria-label': 'Switch to the ' + next + ' theme',
        onclick: function () { setPref('dtheme:' + R().id + ':' + d.name, next); keepPlace(render); } }, icon(t === 'light' ? 'sun' : 'moon'));
    }
    var now = boardTheme();
    return h('button', { class: 'icon-btn theme-btn', title: 'Theme: ' + now, 'aria-label': 'Change theme',
      onclick: function () { var o = ['auto', 'light', 'dark']; setPref('theme:' + R().id, o[(o.indexOf(boardTheme()) + 1) % 3]); applyTheme(); keepPlace(render); } },
      icon(now === 'light' ? 'sun' : now === 'dark' ? 'moon' : 'auto'));
  }
  // header: project icon · title | device widths · theme · Tailnet/here.now
  function renderBar(r) {
    var st = R(), d = r.design, ws = widths();
    var widthSeg = null;
    if (ws.length > 1) {
      widthSeg = seg(ws.map(function (w) { return { value: w, label: wlabel(w), icon: WICON[w], title: wlabel(w) + ' width' }; }),
        S.width, function (v) { if (v !== S.width) { S.width = v; keepPlace(render); } }, 'Mock width');
      widthSeg.classList.add('wseg');
    }
    var chip = st.mode === 'herenow'
      ? h('span', { class: 'badge badge-public', title: 'Hosted on here.now' }, icon('globe'), h('span', { class: 'bl', text: 'here.now' }))
      : st.mode === 'local'
      ? h('span', { class: 'badge badge-private', title: 'Served from this computer at its local address, no Tailscale' }, icon('home'), h('span', { class: 'bl', text: 'Local' }))
      : h('span', { class: 'badge badge-private', title: 'Served from the board server, reachable on the tailnet only' }, icon('lock'), h('span', { class: 'bl', text: 'Tailnet' }));
    var bar = h('header', { class: 'dzbar', id: 'dzbar' },
      projectIcon(r),
      h('div', { class: 'pb-title' }, h('strong', { text: st.title }), h('span', { class: 'pb-sub', text: (d ? d.label + ' · ' : '') + 'Round ' + r.n + (r.kind === 'prototype' ? ' · prototype' : '') })),
      h('span', { class: 'spacer' }), widthSeg, themeControl(r), chip);
    if (window.ResizeObserver) new ResizeObserver(function () { document.documentElement.style.setProperty('--barh', bar.offsetHeight + 'px'); }).observe(bar);
    return bar;
  }

  // footer (sticky bottom): rounds · tally · Send. A message row appears above it for errors / missing notes.
  var footMsg = null;
  function renderFoot(r) {
    var st = R();
    var rounds = h('nav', { class: 'rounds', 'aria-label': 'Rounds' }, st.rounds.map(function (x) {
      var sent = !!submittedFeedback(x);
      return h('button', { class: 'round-tab' + (x.n === S.view ? ' on' : ''), title: 'Round ' + x.n + (x.kind === 'prototype' ? ' · prototype' : ''),
        onclick: function () { if (x.n !== S.view) switchRound(x.n); } },
        h('span', { text: 'R' + x.n }), x.n === st.current_round && !sent ? h('span', { class: 'dot-live' }) : null,
        sent ? h('span', { class: 'tick', title: 'Feedback sent' }, icon('check')) : null);
    }));
    footMsg = h('div', { class: 'ft-msg', id: 'ft-msg', role: 'alert' });
    footMsg.hidden = true;
    sendbar = h('div', { class: 'sendbar sendbar-inline' });
    var foot = h('footer', { class: 'dzfoot', id: 'dzfoot' }, footMsg,
      h('div', { class: 'ft-row' }, rounds, renderTally(r), h('span', { class: 'spacer' }), sendbar));
    updateSendbar();
    requestAnimationFrame(updateTally);
    if (window.ResizeObserver) new ResizeObserver(function () { document.documentElement.style.setProperty('--footh', foot.offsetHeight + 'px'); }).observe(foot);
    return foot;
  }

  function renderNew(r) {
    var seq = ++renderSeq;
    MOCKS = [];
    var main = h('main', { class: 'dz dz-' + r.kind }, h('div', { class: 'boot' }, h('div', { class: 'boot-dot' }), h('p', { text: 'Loading round ' + r.n + '…' })));
    append(app, [renderBar(r), renderStatusBanner(), main, renderFoot(r)]);
    loadAssets(r).then(function (a) {
      if (seq !== renderSeq) return;
      main.textContent = '';
      append(main, r.kind === 'prototype' ? renderProto(r) : renderDoc(r, a));
      lastNarrow = narrow();
    }).catch(function (e) {
      if (seq !== renderSeq) return;
      main.textContent = '';
      append(main, h('div', { class: 'boot error' }, h('h1', { text: 'This round could not be loaded' }), h('p', { text: String(e.message || e) }),
        h('button', { class: 'btn', onclick: function () { delete ASSETS[r.n]; render(); }, text: 'Try again' })));
    });
  }
  var lastNarrow = narrow(), rzTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(rzTimer);
    rzTimer = setTimeout(function () { if (S.review && isNew() && narrow() !== lastNarrow && !pop) keepPlace(render); }, 150);
  });

  // ---- the decision doc ----
  function paras(text, cls) {
    return String(text || '').split(/\n{2,}/).filter(function (x) { return x.trim(); }).map(function (x) { return h('p', { class: cls || null, text: x.trim() }); });
  }
  function renderDoc(r, a) {
    var dec = a.dec, st = R();
    var doc = h('div', { class: 'doc' + (isEditable() ? '' : ' ro') });
    append(doc, h('header', { class: 'doc-head' },
      h('div', { class: 'eyebrow', text: [(r.design && r.design.label), 'Round ' + r.n, wlabel(S.width) + ' mocks'].filter(Boolean).join(' · ') }),
      h('h1', { text: st.title }),
      paras(dec.intro || st.brief),
      isEditable() && pinsOn() ? h('p', { class: 'hint-line' }, icon('pin'), 'Tap any mock to pin a comment on that spot.') : null));
    append(doc, renderDocNav(r));
    dec.blocks.forEach(function (b) { append(doc, renderBlock(r, a, b)); });
    append(doc, renderElse());
    var idx = r.decisions || [];
    append(doc, h('div', { class: 'doc-end', id: 'dz-end' }));
    requestAnimationFrame(updateTally);
    void idx;
    DOC = { r: r, dec: dec };
    return doc;
  }
  var DOC = null;

  function renderTally(r) {   // lives in the footer
    var idx = r.decisions || [];
    function count(id, color, long, short) {
      return h('span', { class: 't-count', title: long.trim() }, h('span', { class: 't-dot', style: { background: color } }), h('b', { id: id, text: '0' }),
        h('span', { class: 'tl', text: long }), h('span', { class: 'ts', text: short }));
    }
    return h('div', { class: 'tally', id: 'dz-tally', 'aria-label': 'Decisions so far' },
      count('c-approve', 'var(--ok)', ' approved', ''), count('c-change', 'var(--warn)', ' to change', ''), count('c-skip', 'var(--faint)', ' skipped', ''),
      h('span', { class: 't-count t-open' }, h('b', { id: 'c-open', text: String(idx.length) }),
        h('span', { class: 'tl', text: ' of ' + idx.length + ' open' }), h('span', { class: 'ts', text: '/' + idx.length + ' open' })));
  }
  function renderDocNav(r) {   // decision chips: jump to D1, D2 … (green once marked)
    var idx = r.decisions || [];
    if (!idx.length) return null;
    return h('nav', { class: 'dnav t-nav', 'aria-label': 'Decisions' }, idx.map(function (d) {
      return h('a', { href: '#dz-' + d.id, 'data-id': d.id, text: d.nav || (d.id.toUpperCase() + (d.title ? ' · ' + d.title.slice(0, 22) : '')),
        onclick: function (e) { e.preventDefault(); var t = document.getElementById('dz-' + d.id); if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
    }));
  }
  function updateTally() {
    if (!document.getElementById('dz-tally')) return;
    var idx = viewed().decisions || [], c = { approve: 0, change: 0, skip: 0 };
    idx.forEach(function (d) { var s = mark(d.id).status; if (c[s] !== undefined) c[s]++; });
    ['approve', 'change', 'skip'].forEach(function (k) { document.getElementById('c-' + k).textContent = c[k]; });
    var open = idx.length - c.approve - c.change - c.skip;
    document.getElementById('c-open').textContent = open;
    document.querySelectorAll('.t-nav a').forEach(function (a) { a.classList.toggle('done', !!mark(a.dataset.id).status); });
    var end = document.getElementById('dz-end');
    if (end && DOC) end.textContent = open ? open + ' of ' + idx.length + ' still open.' + (DOC.dec.next ? ' Once all are decided → ' + DOC.dec.next : '')
      : 'All ' + idx.length + ' decided.' + (DOC.dec.next ? ' Next → ' + DOC.dec.next : '') + (isEditable() ? ' Press Send when you’re ready.' : '');
  }

  var STATUS_TEXT = { approve: 'Approved', change: 'Change', skip: 'Skipped' };
  var TONES = ['neutral', 'ok', 'warn', 'err', 'accent'];
  var TONE_ICON = { neutral: 'info', accent: 'info', ok: 'check', warn: 'alert', err: 'stop' };
  function tone(t) { return TONES.indexOf(t) >= 0 ? t : 'neutral'; }
  function renderBlock(r, a, b) {
    if (b.type === 'text') {
      return h('section', { class: 'blk blk-text' }, b.title ? h('h2', { text: b.title }) : null, paras(b.text));
    }
    if (b.type === 'cards') return renderCards(b);
    if (b.type === 'callout') return renderCallout(b);
    if (b.type === 'table') return renderTable(b);
    if (b.type === 'mock') return renderContextMock(r, a, b);
    if (b.type !== 'decision' && b.type !== 'recap') return null;
    var el = h('section', { class: 'blk item', id: 'dz-' + b.id, 'data-id': b.id });
    var head = h('div', { class: 'ihead' }, b.tag ? h('span', { class: 'tag', text: b.tag }) : null,
      h('h3', { text: b.title || (b.type === 'recap' ? 'Is this record right?' : b.id.toUpperCase()) }), h('span', { class: 'status' }));
    append(el, head);
    if (b.type === 'recap') {
      append(el, h('ul', { class: 'recap' }, b.items.map(function (x) { return h('li', { text: x }); })));
      if (b.markable !== false) append(el, decideRow(b, false));   // missing flag = markable, same default as store.validate_decisions
    } else {
      if (b.today) append(el, h('p', { class: 'today' }, h('b', { text: 'Today: ' }), b.today));
      if (b.text) append(el, paras(b.text, 'itext'));
      append(el, renderOptions(r, a, b));
      append(el, decideRow(b, pickable(b)));
      append(el, h('ol', { class: 'ipins', id: 'pins-' + b.id }));
    }
    setTimeout(function () { refreshBlock(b.id); }, 0);
    return el;
  }
  // ---- context sections: nothing to vote on, never in the tally / nav / marks ----
  function renderCards(b) {   // "what's changing": a grid of short cards, 1 column on a phone, 2-3 on desktop
    return h('section', { class: 'blk blk-cards' }, b.title ? h('h2', { text: b.title }) : null,
      h('div', { class: 'cards' }, b.items.map(function (c) {
        return h('article', { class: 'card tone-' + tone(c.tone) },
          h('div', { class: 'card-head' }, h('h3', { text: c.title }), c.pill ? h('span', { class: 'pill', text: c.pill }) : null),
          c.text ? paras(c.text, 'card-text') : null);
      })));
  }
  function renderCallout(b) {   // one highlighted statement: a constraint, a risk, a heads-up
    var t = tone(b.tone);
    return h('section', { class: 'blk blk-callout' },
      h('div', { class: 'callout tone-' + t, role: 'note' }, icon(TONE_ICON[t], 'callout-ico'),
        h('div', { class: 'callout-body' }, b.title ? h('strong', { class: 'callout-title', text: b.title }) : null, paras(b.text))));
  }
  function renderTable(b) {   // scrolls sideways inside its own card on a phone; the first column stays put
    var hl = b.highlight_col;
    function cls(j) { return [j === 0 ? 'c0' : '', j === hl ? 'hl' : ''].join(' ').trim() || null; }
    return h('section', { class: 'blk blk-table' }, b.title ? h('h2', { text: b.title }) : null,
      h('div', { class: 'tbl-wrap', tabindex: '0', role: 'region', 'aria-label': b.title || 'Table' },
        h('table', { class: 'tbl' },
          h('thead', null, h('tr', null, b.columns.map(function (c, j) { return h('th', { scope: 'col', class: cls(j), text: c }); }))),
          h('tbody', null, b.rows.map(function (row) {
            return h('tr', null, row.map(function (c, j) {
              var cell = h(j ? 'td' : 'th', { scope: j ? null : 'row', class: cls(j) });
              var url = safeLink(c);
              if (url) { var a = h('a', { text: c }); secureLink(a, url); cell.appendChild(a); }
              else cell.textContent = c;
              return cell;
            }));
          })))));
  }
  function renderContextMock(r, a, b) {   // a real app mock for context ("today"): pins yes, vote no
    var el = h('section', { class: 'blk ctx', id: 'dz-' + b.id, 'data-id': b.id },
      h('div', { class: 'ihead' }, h('span', { class: 'tag tag-ctx', text: 'For context' }), b.title ? h('h3', { text: b.title }) : null),
      b.text ? paras(b.text, 'itext') : null,
      h('figure', { class: 'opt ctx-fig' }, mockBox(r, a, b, { id: null, html: b.html }), b.caption ? h('figcaption', { class: 'ctx-cap', text: b.caption }) : null),
      h('ol', { class: 'ipins', id: 'pins-' + b.id }));
    setTimeout(function () { redrawPins(b.id); }, 0);
    return el;
  }

  function firstShown(b) {   // one-at-a-time: open on the Recommended option (the toggle keeps the A, B, C… order)
    var rec = b.options.filter(function (o) { return o.recommended; })[0];
    return (rec || b.options[0]).id;
  }
  function pickable(b) {   // before-after with 2+ "afters" needs a Pick too (which after?)
    return b.layout === 'side' || b.layout === 'switch' || (b.layout === 'before-after' && b.options.length > 2);
  }
  function renderOptions(r, a, b) {
    var oneAtATime = b.layout === 'switch' || (b.layout === 'side' && narrow() && b.options.length > 1);
    var wrap = h('div', { class: 'opts opts-' + b.layout + ' w-' + S.width + ' n' + b.options.length + (oneAtATime ? ' one' : '') });
    var opts = b.options;
    var cur = S.show[b.id] || mark(b.id).option || firstShown(b);
    if (oneAtATime) {
      var toggle = h('div', { class: 'seg oseg', role: 'tablist', 'aria-label': 'Options' }, opts.map(function (o) {
        return h('button', { class: 'seg-btn' + (o.id === cur ? ' on' : ''), role: 'tab', 'aria-selected': String(o.id === cur), 'data-o': o.id,
          onclick: function (ev) { S.show[b.id] = o.id; showOpt(b.id, o.id, ev.currentTarget); } },
          h('span', { text: o.id.toUpperCase() }), o.recommended ? h('span', { class: 'rec-dot', title: 'Recommended' }) : null);
      }));
      append(wrap, toggle);
    }
    var row = h('div', { class: 'opts-row' });
    opts.forEach(function (o, i) {
      var cap = b.layout === 'steps'
        ? h('figcaption', { class: 'opt-cap' }, h('span', { class: 'opt-letter', text: String(i + 1) }), h('b', { text: o.label }), o.text ? h('span', { class: 'opt-text', text: o.text }) : null)
        : b.layout === 'before-after'
          ? h('figcaption', { class: 'opt-cap' }, h('span', { class: 'ba ' + (i ? 'after' : 'before'), text: i ? 'After' + (opts.length > 2 ? ' ' + o.id.toUpperCase() : '') : 'Before' }),
              h('b', { text: o.label }), o.recommended ? h('span', { class: 'rec', text: 'Recommended' }) : null, o.text ? h('span', { class: 'opt-text', text: o.text }) : null)
          : h('figcaption', { class: 'opt-cap' }, h('span', { class: 'opt-letter', text: o.id.toUpperCase() }), h('b', { text: o.label }),
              o.recommended ? h('span', { class: 'rec', text: 'Recommended' }) : null, o.text ? h('span', { class: 'opt-text', text: o.text }) : null);
      var fig = h('figure', { class: 'opt', 'data-o': o.id }, cap, mockBox(r, a, b, o));
      if (oneAtATime && o.id !== cur) fig.hidden = true;
      row.appendChild(fig);
    });
    append(wrap, row);
    if (b.layout === 'steps' && opts.length > 1) {   // the row scrolls sideways; say so when it doesn't all fit (macOS hides scrollbars)
      var more = h('p', { class: 'steps-more', text: 'Scroll sideways for all ' + opts.length + ' steps →' });
      more.hidden = true;
      append(wrap, more);
      var upd = function () { more.hidden = row.scrollWidth <= row.clientWidth + 2; };
      requestAnimationFrame(upd);
      if (window.ResizeObserver) new ResizeObserver(upd).observe(row);
    }
    return wrap;
  }
  function showOpt(id, oid, anchorEl) {
    var el = document.getElementById('dz-' + id); if (!el) return;
    var top0 = anchorEl ? anchorEl.getBoundingClientRect().top : null;   // keep the tapped control under the finger
    el.querySelectorAll('.oseg .seg-btn').forEach(function (x) { var on = x.dataset.o === oid; x.classList.toggle('on', on); x.setAttribute('aria-selected', String(on)); });
    el.querySelectorAll('.opts.one figure.opt').forEach(function (f) { f.hidden = f.dataset.o !== oid; });
    MOCKS.forEach(function (m) { if (m.b.id === id) fit(m); });
    if (anchorEl && top0 !== null) { var d = anchorEl.getBoundingClientRect().top - top0; if (Math.abs(d) > 1) window.scrollBy(0, d); }
  }

  function mockBox(r, a, b, o) {
    var w = mockWidth(b);
    var outer = h('div', { class: 'mock' });
    var inner = h('div', { class: 'mock-inner', style: { width: w + 'px' } });
    var hostEl = h('div', { class: 'mock-host' });
    var pins = h('div', { class: 'pin-layer' });
    inner.appendChild(hostEl); inner.appendChild(pins); outer.appendChild(inner);
    var sr = hostEl.attachShadow({ mode: 'open' });
    if (a.sheet && 'adoptedStyleSheets' in sr) sr.adoptedStyleSheets = [a.sheet];
    else { var stl = document.createElement('style'); stl.textContent = a.css; sr.appendChild(stl); }
    var cq = document.createElement('div'); cq.className = 'cq';
    var root = document.createElement('div'); root.className = 'app';
    var t = designTheme(r); if (t) root.setAttribute('data-theme', t);
    root.appendChild(cleanFragment(o.html));
    cq.appendChild(root); sr.appendChild(cq);
    var m = { r: r, b: b, o: o, w: w, outer: outer, inner: inner, pins: pins, scale: 1 };
    MOCKS.push(m);
    inner.addEventListener('click', function (e) { mockClick(m, e); });
    if (window.ResizeObserver) { var ro = new ResizeObserver(function () { fit(m); }); ro.observe(outer); ro.observe(inner); }
    requestAnimationFrame(function () { fit(m); drawPins(m); });
    return outer;
  }
  function fit(m) {
    var avail = m.outer.clientWidth; if (!avail) return;
    var s = Math.min(1, avail / m.w);
    m.scale = s;
    m.inner.style.transform = s < 1 ? 'scale(' + s + ')' : '';
    m.outer.classList.toggle('center', s >= 1);
    var hgt = Math.ceil(m.inner.offsetHeight * s) + 'px';
    if (m.outer.style.height !== hgt) m.outer.style.height = hgt;
    m.pins.style.setProperty('--k', String(1 / s));
  }
  function drawPins(m) {
    m.pins.textContent = '';
    if (!pinsOn()) return;
    S.draft.pins.forEach(function (p, i) {
      if (p.block !== m.b.id || p.option !== m.o.id || p.w !== S.width) return;
      m.pins.appendChild(h('button', { class: 'mpin', style: { left: (p.x * 100) + '%', top: (p.y * 100) + '%' }, title: p.text, 'aria-label': 'Pin ' + (i + 1) + ': ' + p.text,
        onclick: function (e) { e.stopPropagation(); editPin(m, i, e); } }, h('span', { text: String(i + 1) })));
    });
  }
  function redrawPins(id) {
    MOCKS.forEach(function (m) { if (m.b.id === id) drawPins(m); });
    var list = document.getElementById('pins-' + id);
    if (list) {
      list.textContent = '';
      S.draft.pins.forEach(function (p, i) {
        if (p.block !== id) return;
        list.appendChild(h('li', null, h('button', { class: 'pin-row', onclick: function () { focusDzPin(i); } },
          h('span', { class: 'pin-num', text: String(i + 1) }),
          h('span', { class: 'pin-body' }, h('span', { class: 'pin-where', text: (p.option ? p.option.toUpperCase() + ' · ' : '') + wlabel(p.w) }), h('span', { class: 'pin-text', text: p.text })))));
      });
    }
    updateSendbar();
  }
  function focusDzPin(i) {
    var p = S.draft.pins[i]; if (!p) return;
    if (p.w !== S.width && widths().indexOf(p.w) >= 0) { S.width = p.w; render(); setTimeout(function () { focusDzPin(i); }, 300); return; }
    if (p.option && document.querySelector('#dz-' + p.block + ' .opts.one')) { S.show[p.block] = p.option; showOpt(p.block, p.option); }
    if (p.block === 'proto') { var fr = S.frames[0]; if (fr && fr.ctx === 'proto') try { fr.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'focus', i: i }, '*'); } catch (e) {} return; }
    var m = MOCKS.filter(function (x) { return x.b.id === p.block && x.o.id === p.option; })[0]; if (!m) return;
    var pin = m.pins.querySelectorAll('.mpin')[[].slice.call(S.draft.pins).filter(function (q, j) { return j < i && q.block === p.block && q.option === p.option && q.w === p.w; }).length];
    (pin || m.outer).scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (pin) setTimeout(function () { pin.classList.add('flash'); setTimeout(function () { pin.classList.remove('flash'); }, 900); }, 350);
  }
  function mockClick(m, e) {
    // Events crossing the shadow root retarget to the host; inspect the actual path.
    var path = e.composedPath ? e.composedPath() : [e.target];
    if (path.some(function (el) { return el.closest && el.closest('a[href],.mpin'); })) return;
    tapMock(m, e);
  }
  function tapMock(m, e) {
    if (!isEditable() || !pinsOn()) return;
    var rc = m.inner.getBoundingClientRect();
    var x = num((e.clientX - rc.left) / rc.width, 0, 1), y = num((e.clientY - rc.top) / rc.height, 0, 1);
    if (S.draft.pins.length >= LIMIT.pins) { toast('That’s ' + LIMIT.pins + ' pins — use the notes for the rest.', 'warn'); return; }
    var ghost = h('div', { class: 'mpin ghost', style: { left: (x * 100) + '%', top: (y * 100) + '%' } }, h('span', { text: String(S.draft.pins.length + 1) }));
    m.pins.appendChild(ghost);
    notePop({ title: 'New pin · ' + pinWhat(m.b, m.o.id), where: wlabel(S.width) + ' · ~' + pct(x) + ', ' + pct(y) + ' of the mock',
      anchor: { x: e.clientX, y: e.clientY }, text: '', isNew: true,
      onClose: function () { ghost.remove(); },
      onSave: function (text) { S.draft.pins.push({ block: m.b.id, option: m.o.id, w: S.width, x: x, y: y, text: text }); saveDraft(); redrawPins(m.b.id); } });
  }
  function pinWhat(b, oid) { return (b.tag || b.id.toUpperCase()) + (oid ? ' · ' + oid.toUpperCase() : ''); }
  function editPin(m, i, e) {
    var p = S.draft.pins[i];
    notePop({ title: 'Pin ' + (i + 1) + ' · ' + pinWhat(m.b, p.option), where: wlabel(p.w) + ' · ~' + pct(p.x) + ', ' + pct(p.y),
      anchor: { x: e.clientX, y: e.clientY }, text: p.text,
      onSave: function (text) { p.text = text; saveDraft(); redrawPins(p.block); },
      onDelete: function () { S.draft.pins.splice(i, 1); saveDraft(); MOCKS.forEach(drawPins); redrawPins(p.block); } });
  }
  // generic pin popover (desktop: next to the tap; phone: bottom sheet)
  function notePop(o) {
    closePopover();
    var editable = isEditable(), ta = null;
    var body = editable ? (ta = h('textarea', { class: 'field', rows: '3', maxlength: String(LIMIT.pin), placeholder: 'What should change here?', 'aria-label': 'Pin comment' }))
      : h('p', { class: 'pop-text', text: o.text });
    if (ta) ta.value = o.text || '';
    function save() {
      var text = ta.value.replace(/\s+/g, ' ').trim();
      if (!text) { ta.focus(); ta.classList.add('shake'); setTimeout(function () { ta.classList.remove('shake'); }, 400); return; }
      var cb = o.onSave; o.onClose = o.onClose || null; closePopover(); cb(text.slice(0, LIMIT.pin));
    }
    var actions = editable
      ? h('div', { class: 'pop-actions' }, o.onDelete ? h('button', { class: 'btn btn-ghost btn-danger', onclick: function () { var cb = o.onDelete; closePopover(); cb(); }, text: 'Delete' }) : null,
          h('span', { class: 'spacer' }), h('button', { class: 'btn btn-ghost', onclick: closePopover, text: 'Cancel' }),
          h('button', { class: 'btn btn-primary', onclick: save, text: o.isNew ? 'Add pin' : 'Save' }))
      : h('div', { class: 'pop-actions' }, h('span', { class: 'spacer' }), h('button', { class: 'btn btn-ghost', onclick: closePopover, text: 'Close' }));
    var el = h('div', { class: 'popover', role: 'dialog', 'aria-label': o.title },
      h('div', { class: 'pop-head' }, h('span', { class: 'pin pin-static' }, icon('pin')), h('div', null, h('strong', { text: o.title }), h('span', { class: 'pop-where', text: o.where }))),
      body, actions);
    if (ta) ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey || !e.shiftKey)) { e.preventDefault(); save(); } });
    document.body.appendChild(el);
    pop = { el: el, f: null, onClose: o.onClose };
    var sheet = window.innerWidth < 640;
    el.classList.toggle('sheet', sheet);
    if (!sheet) {
      var w = el.offsetWidth, hh = el.offsetHeight, left = o.anchor.x + 18;
      if (left + w > window.innerWidth - 12) left = o.anchor.x - w - 18;
      el.style.left = Math.max(12, left) + 'px';
      el.style.top = Math.min(Math.max(12, o.anchor.y - 28), window.innerHeight - hh - 12) + 'px';
    }
    if (ta) setTimeout(function () { ta.focus({ preventScroll: true }); }, 30);
  }

  function decideRow(b, pickable) {
    var ed = isEditable();
    var row = h('div', { class: 'decide', 'data-id': b.id });
    if (pickable && b.options.length > 1) {
      append(row, h('div', { class: 'grp grp-pick', role: 'group', 'aria-label': 'Pick an option' }, h('span', { class: 'lbl', text: 'Pick' }),
        b.options.map(function (o) {
          return h('button', { class: 'dbtn vbtn', 'data-o': o.id, 'aria-pressed': 'false', disabled: ed ? null : true, title: o.label,
            onclick: function () { var on = mark(b.id).option === o.id; setMark(b.id, { option: on ? null : o.id }); } }, o.id.toUpperCase());   // pick records only: never swaps the shown mock (that made the page jump on phones)
        })));
    }
    append(row, h('div', { class: 'grp', role: 'group', 'aria-label': 'Decision' }, ['approve', 'change', 'skip'].map(function (s) {
      return h('button', { class: 'dbtn sbtn s-' + s, 'data-s': s, 'aria-pressed': 'false', disabled: ed ? null : true,
        onclick: function () { setMark(b.id, { status: mark(b.id).status === s ? null : s }); } }, s === 'approve' ? 'Approve' : s === 'change' ? 'Change' : 'Skip');
    })));
    var ta = h('textarea', { class: 'dnote', rows: '1', maxlength: '2000', placeholder: b.type === 'recap' ? 'Note: what’s wrong here?' : 'Note: what to change, or why', 'aria-label': 'Note', readonly: ed ? null : true });
    ta.value = mark(b.id).note || '';
    ta.addEventListener('input', function () { setMarkQuiet(b.id, ta.value); autoGrow(ta); });
    requestAnimationFrame(function () { autoGrow(ta); });
    append(row, ta);
    var need = h('p', { class: 'need-note', role: 'alert', text: 'This Change needs a note: say what to change before you Send.' });
    need.hidden = true;
    append(row, need);
    return row;
  }
  function setMarkQuiet(id, note) {   // typing: don't rebuild anything, just store
    var m = Object.assign({}, mark(id), { note: note });
    if (!m.status && !m.option && !note.trim()) delete S.draft.marks[id]; else S.draft.marks[id] = m;
    saveDraft(); updateSendbar(); updateNeedNote(id);
  }
  function updateNeedNote(id) {
    var el = document.getElementById('dz-' + id); if (!el) return;
    var m = mark(id), need = CFG.require_note_on_change && isEditable() && m.status === 'change' && !(m.note || '').trim();
    el.classList.toggle('needs-note', !!need);
    var nn = el.querySelector('.need-note'); if (nn) nn.hidden = !need;
  }
  function refreshBlock(id) {
    var el = document.getElementById('dz-' + id); if (!el) return;
    var m = mark(id);
    el.querySelectorAll('.sbtn').forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.s === m.status)); });
    el.querySelectorAll('.vbtn').forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.o === m.option)); });
    el.querySelectorAll('figure.opt').forEach(function (f) { f.classList.toggle('picked', f.dataset.o === m.option); });
    var st = el.querySelector('.status');
    st.dataset.s = m.status || '';
    st.textContent = m.status ? STATUS_TEXT[m.status] + (m.option ? ' · ' + m.option.toUpperCase() : '') : m.option ? 'Picked ' + m.option.toUpperCase() : 'Open';
    el.classList.toggle('marked', !!m.status);
    updateNeedNote(id);
    redrawPins(id);
  }
  function renderElse() {
    var ed = isEditable();
    var ta = h('textarea', { class: 'field', rows: '2', maxlength: String(LIMIT.guidelines), placeholder: 'Anything that isn’t about one decision: direction for the next round, things to drop…', 'aria-label': 'Anything else', readonly: ed ? null : true });
    ta.value = S.draft.guidelines || '';
    ta.addEventListener('input', function () { S.draft.guidelines = ta.value; saveDraft(); updateSendbar(); autoGrow(ta); });
    requestAnimationFrame(function () { autoGrow(ta); });
    return h('section', { class: 'blk blk-else' }, h('h2', { text: 'Anything else?' }), ta);
  }

  // ---- prototype round: one clickable page fills the canvas under the bar ----
  function renderProto(r) {
    var v = r.variants[0];
    var f = { r: r, vid: 'a', device: S.width, ctx: 'proto', W: 0, H: 0, pageH: 0, ghost: null, alive: false, scale: 1 };
    f.iframe = h('iframe', { src: v.file, title: R().title + ' prototype', sandbox: 'allow-scripts', referrerpolicy: 'no-referrer' });
    f.el = f.iframe;
    S.frames = [f];
    if (!pinsOn()) S.tool = 'interact';
    var tools = isEditable() && pinsOn() ? seg([{ value: 'interact', label: 'Use it', icon: 'hand' }, { value: 'comment', label: 'Pin', icon: 'pin' }],
      S.tool, function (t) { S.tool = t; keepPlace(render); }, 'Tool') : null;
    var b = { id: 'proto', type: 'recap', options: [] };
    var foot = h('div', { class: 'proto-foot' }, tools, decideRow(b, false), h('span', { class: 'status', id: 'proto-status' }));
    if (S.width !== 'desktop') f.iframe.style.width = (S.width === 'phone' ? CFG.mobile_width : CFG.tablet_width) + 'px';
    var stage = h('div', { class: 'proto w-' + S.width }, f.iframe);
    var wrap = h('div', { class: 'proto-wrap', id: 'dz-proto' }, stage, foot, h('ol', { class: 'ipins', id: 'pins-proto' }));
    setTimeout(function () { refreshBlock('proto'); }, 0);
    return wrap;
  }
  function pushProto(f) {
    var out = [];
    S.draft.pins.forEach(function (p, i) { if (p.block === 'proto' && p.w === f.device) out.push({ i: i, n: i + 1, x: p.x, y: p.y }); });
    if (f.ghost) out.push({ i: -1, n: S.draft.pins.length + 1, x: f.ghost.x, y: f.ghost.y, ghost: true });
    try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'state', comment: isEditable() && pinsOn() && S.tool === 'comment', scale: 1, pins: pinsOn() ? out : [] }, '*'); } catch (e) {}
  }
  function protoMessage(f, d) {
    var rc = f.iframe.getBoundingClientRect();
    if (d.type === 'hello') { f.alive = true; pushProto(f); return; }
    if (d.type === 'click') {
      if (!isEditable() || !pinsOn() || S.tool !== 'comment') return;
      var x = num(d.x, 0, 1), y = num(d.y, 0, 1);
      f.ghost = { x: x, y: y }; pushProto(f);
      notePop({ title: 'New pin · prototype', where: wlabel(f.device) + ' · ~' + pct(x) + ', ' + pct(y) + ' of the page', isNew: true,
        anchor: { x: rc.left + num(d.cx, 0, 4000), y: rc.top + num(d.cy, 0, 4000) }, text: '',
        onClose: function () { f.ghost = null; pushProto(f); },
        onSave: function (text) { S.draft.pins.push({ block: 'proto', option: null, w: f.device, x: x, y: y, text: text }); saveDraft(); f.ghost = null; pushProto(f); redrawPins('proto'); } });
    } else if (d.type === 'pin') {
      var i = num(d.i, -1, 99) | 0, p = S.draft.pins[i]; if (!p) return;
      notePop({ title: 'Pin ' + (i + 1) + ' · prototype', where: wlabel(p.w) + ' · ~' + pct(p.x) + ', ' + pct(p.y),
        anchor: { x: rc.left + num(d.cx, 0, 4000), y: rc.top + num(d.cy, 0, 4000) }, text: p.text,
        onSave: function (text) { p.text = text; saveDraft(); pushProto(f); redrawPins('proto'); },
        onDelete: function () { S.draft.pins.splice(i, 1); saveDraft(); pushProto(f); redrawPins('proto'); } });
    }
  }

  // ---------- send bar ----------
  var sendbar = null;
  function renderSendbar() {
    sendbar = h('footer', { class: 'sendbar' });
    updateSendbar();
    return sendbar;
  }
  function summaryChips() {
    var d = S.draft, r = viewed(), chips = [];
    if (isNew(r)) {
      var decided = Object.keys(d.marks).filter(function (k) { return d.marks[k].status; }).length;
      chips.push(h('span', { class: 'sum-pick' + (decided ? '' : ' muted') }, decided ? icon('check') : null, decided ? decided + ' marked' : 'Nothing marked yet'));
      if (d.pins.length) chips.push(h('span', { class: 'sum-count', text: d.pins.length + ' pin' + (d.pins.length === 1 ? '' : 's') }));
      return chips;
    }
    var pins = 0, notes = 0;
    Object.keys(d.variants).forEach(function (k) { pins += d.variants[k].pins.length; if (d.variants[k].note.trim()) notes++; });
    var pickText = d.choice === 'none' ? 'None / combine' : d.choice ? letter(d.choice) + ' · ' + r.variants.filter(function (v) { return v.id === d.choice; })[0].label : 'No pick yet';
    chips.push(h('span', { class: 'sum-pick' + (d.choice ? '' : ' muted') }, d.choice ? icon('check') : null, pickText));
    chips.push(h('span', { class: 'sum-count', text: pins + ' pin' + (pins === 1 ? '' : 's') }));
    if (notes) chips.push(h('span', { class: 'sum-count', text: notes + ' note' + (notes === 1 ? '' : 's') }));
    if (d.guidelines.trim()) chips.push(h('span', { class: 'sum-count', text: 'guidelines' }));
    return chips;
  }
  function setFootMsg(kind, title, text, jumps) {   // the footer's message row (decision rounds only)
    if (!footMsg) return;
    footMsg.textContent = '';
    footMsg.hidden = !title;
    footMsg.className = 'ft-msg' + (kind ? ' ft-' + kind : '');
    if (!title) return;
    append(footMsg, [h('strong', { text: title }), text ? h('span', { text: ' ' + text }) : null].concat(
      (jumps || []).map(function (d) {
        return h('button', { class: 'link-btn', text: d.nav || d.id.toUpperCase(), onclick: function () {
          var el = document.getElementById('dz-' + d.id); if (!el) return;
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          var ta = el.querySelector('.dnote'); if (ta) setTimeout(function () { ta.focus({ preventScroll: true }); }, 350);
        } });
      })));
  }
  function updateSendbar() {
    if (!sendbar) return;
    sendbar.textContent = '';
    setFootMsg(null, '');
    var st = R(), r = viewed();
    var inner = h('div', { class: 'sendbar-inner' });
    sendbar.appendChild(inner);
    sendbar.className = 'sendbar' + (isNew(r) ? ' sendbar-inline' : '');
    if (st.status !== 'open') {
      inner.appendChild(h('div', { class: 'sb-status' }, h('span', { class: 'sb-title', text: 'Review closed' }), h('span', { class: 'sb-sub', text: 'Thanks — nothing more to send here.' })));
      return;
    }
    if (r.n !== st.current_round) {
      inner.appendChild(h('div', { class: 'sb-status' }, h('span', { class: 'sb-title', text: 'Round ' + r.n + ' (read-only)' }), h('span', { class: 'sb-sub', text: 'You’re looking at an earlier round.' })));
      inner.appendChild(h('button', { class: 'btn btn-primary', onclick: function () { switchRound(st.current_round); }, text: 'Go to round ' + st.current_round }));
      return;
    }
    var fb = submittedFeedback(r);
    if (fb && !S.amending) {
      var sub = r.submission, ls = localSent(r.n);
      var title, subline, cls;
      if (sub && sub.status === 'notified') {
        if (sub.stalled_notice_at) {
          title = 'Hermes may have stalled'; cls = 'warn';
          subline = 'Sent ' + fmtDate(sub.submitted_at) + (sub.amended ? ' (amended)' : '') + '. No new round yet — ask in the chat or resend from the board.';
        } else {
          title = 'Sent ✓ — Hermes is on it'; cls = 'ok';
          subline = 'Sent ' + fmtDate(sub.submitted_at) + (sub.amended ? ' (amended)' : '') + ' · Hermes was notified and is working on it.';
        }
      } else if (sub && sub.status === 'pending_notify') {
        title = 'Saved ✓ — still reaching Hermes'; cls = 'warn';
        subline = 'Your feedback is stored. Hermes hasn’t confirmed yet; the board server retries every minute.';
      } else if (ls) {
        title = 'Sent ✓ — Hermes is on it'; cls = 'ok';
        subline = st.submit.kind === 'sitedata'
          ? 'Saved ' + fmtDate(ls.at) + '. Hermes is working on it and will reply soon.'
          : 'Sent ' + fmtDate(ls.at) + '.';
      } else {
        title = 'Feedback sent'; cls = 'ok'; subline = '';
      }
      if (S.error) {
        title = S.error.title; cls = 'err';
        subline = S.error.text;
        setFootMsg('err', S.error.title + '.', S.error.text);
      }
      sendbar.classList.add('sb-' + cls);
      inner.appendChild(h('div', { class: 'sb-status' }, h('span', { class: 'sb-title', text: title }), h('span', { class: 'sb-sub', text: subline })));
      inner.appendChild(h('button', { class: 'btn', onclick: startAmend, text: 'Amend feedback' }));
      return;
    }
    var missing = missingNotes();
    if (S.error) {
      sendbar.classList.add('sb-err');
      if (footMsg) setFootMsg('err', S.error.title + '.', S.error.text);
      else inner.appendChild(h('div', { class: 'sb-status' }, h('span', { class: 'sb-title', text: S.error.title }), h('span', { class: 'sb-sub', text: S.error.text })));
    } else {
      inner.appendChild(h('div', { class: 'sb-summary' }, summaryChips()));
    }
    if (missing.length && !S.error) setFootMsg('warn', 'Add a note to each Change before sending:', '', missing);
    var ready = hasContent(S.draft) && !S.sending && !missing.length;
    inner.appendChild(h('button', {
      class: 'btn btn-primary btn-send', disabled: ready ? null : true, onclick: send,
      title: missing.length ? 'Every Change needs a note first' : hasContent(S.draft) ? 'Send this round’s feedback to Hermes' : (isNew(r) ? 'Mark a decision, pin a mock or write a note first' : 'Pick a variant, drop a pin or write a note first')
    }, S.sending ? h('span', { class: 'spinner' }) : icon('send'), S.sending ? 'Sending…' : (S.amending ? 'Send amendment' : (isNew(r) ? 'Send' : 'Send to Hermes'))));
  }

  function toast(msg, kind) {
    var t = h('div', { class: 'toast toast-' + (kind || 'info'), role: 'status', text: msg });
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add('out'); }, 3200);
    setTimeout(function () { t.remove(); }, 3700);
  }

  // ---------- actions ----------
  function setTool(t) {
    S.tool = t;
    closePopover();
    document.body.classList.toggle('tool-comment', t === 'comment');
    document.body.classList.toggle('tool-interact', t === 'interact');
    var tb = document.getElementById('toolbar');
    if (tb) tb.replaceWith(renderToolbar());
    pushAll();
  }
  function switchRound(n) {
    closeFull(); S.show = {};
    S.view = n; S.amending = false; S.error = null;
    history.replaceState(null, '', n === R().current_round ? location.pathname : '#round-' + n);
    loadDraft(); render(); window.scrollTo({ top: 0 });
  }
  function startAmend() { S.amending = true; S.error = null; loadDraft(); render(); }
  function cancelAmend() { S.amending = false; S.error = null; lsDel(key('amend')); loadDraft(); render(); }

  function send() {
    if (S.sending || !hasContent(S.draft)) return;
    if (missingNotes().length) { updateSendbar(); return; }
    if (S.draft.choice === 'none' && !S.draft.choice_note.trim() && !S.draft.guidelines.trim()) {
      S.error = { title: 'Say what to combine', text: 'You chose “None / combine” — add a line on what to take from which variant.' };
      updateSendbar();
      var f = document.getElementById('f-choice_note'); if (f) { f.focus(); f.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
      return;
    }
    S.error = null; S.sending = true; updateSendbar();
    var r = viewed(), amend = S.amending, body = payload(S.draft);
    var p = R().submit.kind === 'sitedata' ? sendSiteData(r, body, amend) : sendLocal(r, body, amend);
    p.then(function (res) {
      S.sending = false;
      lsSet(key('sent', r.n), { at: new Date().toISOString(), amend: amend, feedback: body });
      lsDel(key('draft', r.n)); lsDel(key('amend', r.n));
      S.amending = false;
      if (res && res.state) S.review = res.state;
      loadDraft(); render();
      toast(res && res.notified === false ? 'Saved — Hermes will be pinged as soon as it answers.' : 'Sent ✓ — Hermes is on it', res && res.notified === false ? 'warn' : 'ok');
      maybePollForNotify();
      maybeWatchForRound();
    }).catch(function (e) {
      S.sending = false;
      S.error = { title: e.title || 'Couldn’t send', text: e.message || String(e) };
      updateSendbar();
      if (e.refresh) fetchState().then(function (st) { S.review = st; updateSendbar(); }).catch(function () {});
    });
  }

  function err(title, message, refresh) { var e = new Error(message); e.title = title; e.refresh = refresh; return e; }

  function sendLocal(r, body, amend) {
    return fetch('api/submit', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ round: r.n, token: R().token, amend: amend, feedback: body })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (res.status === 409) throw err('Already sent', j.error || 'This round already has feedback.', true);
        if (!res.ok) throw err('Couldn’t send', j.error || ('Server answered HTTP ' + res.status));
        return fetchState().then(function (st) { return { notified: j.notified, state: st }; });
      });
    }, function () { throw err('Offline?', 'The board server didn’t answer. Your draft is saved on this device — try again in a moment.'); });
  }

  function sendSiteData(r, body, amend) {
    var text = JSON.stringify(body);
    var max = (R().submit.max_payload || 15000);
    if (text.length > max) return Promise.reject(err('Too long for a public review', 'Feedback is ' + text.length + ' characters; the limit here is ' + max + '. Trim a few notes.'));
    return fetch('.herenow/data/' + (R().submit.collection || 'feedback'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': S.draft.idem || uuid() },
      body: JSON.stringify({ token: R().token, round: r.n, amend: amend, payload: text })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (res.status === 429) throw err('Slow down', 'Too many submits from this network. Wait a few minutes and send again.');
        if (!res.ok) throw err('Couldn’t send', (j && (j.message || j.error)) || ('here.now answered HTTP ' + res.status));
        return { notified: true };
      });
    }, function () { throw err('Offline?', 'Couldn’t reach here.now. Your draft is saved on this device.'); });
  }

  // After a here.now submit the board server republishes state.json once Hermes is notified; poll for it.
  function maybePollForNotify() {
    clearTimeout(S.pollTimer);
    var st = R(), r = cur();
    var ls = localSent(r.n);
    var waiting = (r.submission && r.submission.status === 'pending_notify') || (ls && !r.submission && st.submit.kind === 'sitedata') ||
      (ls && r.submission && ls.amend && Date.parse(r.submission.submitted_at) < Date.parse(ls.at) - 60000);
    if (!waiting || st.status !== 'open') return;
    var tries = 0;
    (function tick() {
      S.pollTimer = setTimeout(function () {
        tries++;
        fetchState().then(function (fresh) {
          var before = JSON.stringify(cur().submission);
          S.review = fresh;
          if (JSON.stringify(cur().submission) !== before && S.view === fresh.current_round && !S.amending) updateSendbar();
          var sub = cur().submission;
          if (sub && sub.status === 'notified') maybeWatchForRound();
          else if (tries < 30) tick();
        }).catch(function () { if (tries < 30) tick(); });
      }, 15000);
    })();
  }

  // Slow watch: while the current round is sent and no newer round exists, look at state.json every 30 s (for up to
  // 2 h, and not once the review closes) so the stalled hint and the next round show up without a reload.
  var WATCH_EVERY = 30000, WATCH_FOR = 2 * 3600 * 1000;

  function refreshStatus() {
    var old = app.querySelector('.banner'), nb = renderStatusBanner();
    if (old && nb) old.replaceWith(nb);
    else if (old) old.remove();
    else if (nb && !S.amending) { render(); return; }
    updateSendbar();
  }

  function maybeWatchForRound() {
    clearTimeout(S.watchTimer);
    var st = R(), r = cur(), sub = r.submission;
    if (st.status !== 'open' || !sub || sub.status !== 'notified') return;
    var round = r.n, until = Date.now() + WATCH_FOR;
    (function tick() {
      S.watchTimer = setTimeout(function () {
        if (Date.now() > until) return;
        fetchState().then(function (fresh) {
          var before = cur().submission, hadStalled = !!(before && before.stalled_notice_at);
          S.review = fresh;
          if (fresh.status !== 'open') { if (!S.amending) refreshStatus(); return; }
          if (fresh.current_round > round) {
            toast('Round ' + fresh.current_round + ' is ready', 'ok');
            if (!S.amending) refreshStatus();
            return;
          }
          var now = cur().submission;
          if (now && now.stalled_notice_at && !hadStalled && !S.amending) refreshStatus();
          tick();
        }).catch(tick);
      }, WATCH_EVERY);
    })();
  }

  boot();
})();
