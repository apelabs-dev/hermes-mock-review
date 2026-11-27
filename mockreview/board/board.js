import {LIVE, CONTROL, controlOf, presses, anchorFor, cleanTarget, describeTarget, rangeFor, resolve, stamp, surface, installAPI} from './anchors.js';
import {createThreads} from './threads.js';
import {paintAvatar} from './avatar.js';
/* mock-review board — vanilla JS, no build step, no network beyond this site.
 * Feedback: POST ./api/submit on the board server (local or tailnet).
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
  var LIMIT = { pin: 600, comment: 2000, note: 3000, guidelines: 5000, choice: 2000, pins: 40, custom: 280 };
  // The board's own controls keep working in Comment mode: a click whose path touches one of these never starts a board
  // (canvas) pin. Mocks are listed because their clicks belong to mockClick, which pins a mock's controls too.
  var PIN_SKIP = 'button,textarea,input,select,option,label,a,summary,video,audio,[contenteditable],[data-mr-no-pin],.mpin,.mock,.mock-host,.pin-layer,.ipins,.popover';

  var app = document.getElementById('app');
  var S = {
    review: null,
    view: 1,
    device: pref('device', window.innerWidth < 700 ? 'both' : 'both'),
    // one comment mode shared by decisions, prototype and legacy variants; OFF until the reviewer presses Comment (C)
    commentMode: false,
    draft: null,
    amending: false,
    sending: false,
    error: null,
    frames: [],
    frameTokens: {},
    full: null,
    share: null,
    exportComments: pref('export:comments', '1') !== '0',
    exportMenu: null,
    frameDismiss: 0,
    pollTimer: null,
    watchTimer: null
  };

  // ---------- small helpers ----------
  function pref(k, d) { try { return localStorage.getItem('mr:pref:' + k) || d; } catch (e) { return d; } }
  function setPref(k, v) { try { localStorage.setItem('mr:pref:' + k, v); } catch (e) {} }
  function lsGet(k) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : null; } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }
  function isEl(el) { return el instanceof Element; }
  function closestOf(el, sel) { return isEl(el) ? Element.prototype.closest.call(el, sel) : null; }
  function rectOf(el) { return isEl(el) ? Element.prototype.getBoundingClientRect.call(el) : null; }

  // Who is looking (state.json `me`, served by the board server) and the CSRF token every POST
  // carries: the server sets the mr_csrf cookie and wants the same value back in X-CSRF-Token.
  function me() { return (S.review && S.review.me) || null; }
  function canDo(cap) { var m = me(); return !m || (m.caps || []).indexOf(cap) >= 0; }
  // Send and Build are the owner's alone: the board server answers 403 to anyone else. No `me` is an older board
  // server, which keeps its own rule.
  function ownerSends() { var m = me(); return !m || m.review_role === 'owner'; }
  // Commenting is its own capability: any round, submitted or not, and never gated on the submit role.
  function canComment() { return !!R() && canDo('comment') && pinsOn(); }
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)mr_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify(body)
    });
  }

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
    "pin": "<path d=\"M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0\" /> <circle cx=\"12\" cy=\"10\" r=\"3\" />",
    "close": "<path d=\"M18 6 6 18\" /> <path d=\"m6 6 12 12\" />",
    "check": "<path d=\"M20 6 9 17l-5-5\" />",
    "send": "<path d=\"M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z\" /> <path d=\"m21.854 2.147-10.94 10.939\" />",
    "lock": "<rect width=\"18\" height=\"11\" x=\"3\" y=\"11\" rx=\"2\" ry=\"2\" /> <path d=\"M7 11V7a5 5 0 0 1 10 0v4\" />",
    "home": "<path d=\"M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8\" /> <path d=\"M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z\" />",
    "globe": "<circle cx=\"12\" cy=\"12\" r=\"10\" /> <path d=\"M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20\" /> <path d=\"M2 12h20\" />",
    "sun": "<circle cx=\"12\" cy=\"12\" r=\"4\" /> <path d=\"M12 2v2\" /> <path d=\"M12 20v2\" /> <path d=\"m4.93 4.93 1.41 1.41\" /> <path d=\"m17.66 17.66 1.41 1.41\" /> <path d=\"M2 12h2\" /> <path d=\"M20 12h2\" /> <path d=\"m6.34 17.66-1.41 1.41\" /> <path d=\"m19.07 4.93-1.41 1.41\" />",
    "moon": "<path d=\"M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401\" />",
    "auto": "<path d=\"M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z\" /> <circle cx=\"13.5\" cy=\"6.5\" r=\".5\" fill=\"currentColor\" /> <circle cx=\"17.5\" cy=\"10.5\" r=\".5\" fill=\"currentColor\" /> <circle cx=\"6.5\" cy=\"12.5\" r=\".5\" fill=\"currentColor\" /> <circle cx=\"8.5\" cy=\"7.5\" r=\".5\" fill=\"currentColor\" />",
    "ext": "<path d=\"M15 3h6v6\" /> <path d=\"M10 14 21 3\" /> <path d=\"M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6\" />",
    "note": "<path d=\"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z\" /> <path d=\"M14 2v5a1 1 0 0 0 1 1h5\" /> <path d=\"M10 9H8\" /> <path d=\"M16 13H8\" /> <path d=\"M16 17H8\" />",
    "desktop": "<rect width=\"20\" height=\"14\" x=\"2\" y=\"3\" rx=\"2\" /> <line x1=\"8\" x2=\"16\" y1=\"21\" y2=\"21\" /> <line x1=\"12\" x2=\"12\" y1=\"17\" y2=\"21\" />",
    "phone": "<rect width=\"14\" height=\"20\" x=\"5\" y=\"2\" rx=\"2\" ry=\"2\" /> <path d=\"M12 18h.01\" />",
    "tablet": "<rect width=\"16\" height=\"20\" x=\"4\" y=\"2\" rx=\"2\" ry=\"2\" /> <line x1=\"12\" x2=\"12.01\" y1=\"18\" y2=\"18\" />",
    "info": "<circle cx=\"12\" cy=\"12\" r=\"10\" /> <path d=\"M12 16v-4\" /> <path d=\"M12 8h.01\" />",
    "alert": "<path d=\"m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3\" /> <path d=\"M12 9v4\" /> <path d=\"M12 17h.01\" />",
    "stop": "<path d=\"M18 6 6 18\" /> <path d=\"m6 6 12 12\" />",
    "hand": "<path d=\"M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z\" /> <path d=\"m15 5 4 4\" />",
    "expand": "<path d=\"M15 3h6v6\" /> <path d=\"m21 3-7 7\" /> <path d=\"m3 21 7-7\" /> <path d=\"M9 21H3v-6\" />",
    "more": "<circle cx=\"12\" cy=\"12\" r=\"1\" /> <circle cx=\"19\" cy=\"12\" r=\"1\" /> <circle cx=\"5\" cy=\"12\" r=\"1\" />",
    "share": "<circle cx=\"18\" cy=\"5\" r=\"3\" /> <circle cx=\"6\" cy=\"12\" r=\"3\" /> <circle cx=\"18\" cy=\"19\" r=\"3\" /> <line x1=\"8.59\" x2=\"15.42\" y1=\"13.51\" y2=\"17.49\" /> <line x1=\"15.41\" x2=\"8.59\" y1=\"6.51\" y2=\"10.49\" />",
    "comments": "<path d=\"M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z\" />",
    "settings": "<path d=\"M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915\" /> <circle cx=\"12\" cy=\"12\" r=\"3\" />",
    "signout": "<path d=\"m16 17 5-5-5-5\" /> <path d=\"M21 12H9\" /> <path d=\"M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4\" />",
    "download": "<path d=\"M12 15V3\" /> <path d=\"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4\" /> <path d=\"m7 10 5 5 5-5\" />",
    "copy": "<rect width=\"14\" height=\"14\" x=\"8\" y=\"8\" rx=\"2\" ry=\"2\" /> <path d=\"M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2\" />",
    "search": "<path d=\"m21 21-4.34-4.34\" /> <circle cx=\"11\" cy=\"11\" r=\"8\" />",
    "link": "<path d=\"M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71\" /> <path d=\"M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71\" />",
    "users": "<path d=\"M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2\" /> <path d=\"M16 3.128a4 4 0 0 1 0 7.744\" /> <path d=\"M22 21v-2a4 4 0 0 0-3-3.87\" /> <circle cx=\"9\" cy=\"7\" r=\"4\" />",
    "plus": "<path d=\"M5 12h14\" /> <path d=\"M12 5v14\" />",
    "minus": "<path d=\"M5 12h14\" />",
    "scan": "<path d=\"M4 7V5a1 1 0 0 1 1-1h2m10 0h2a1 1 0 0 1 1 1v2m0 10v2a1 1 0 0 1-1 1h-2M7 20H5a1 1 0 0 1-1-1v-2\" />",
    "grab": "<path d=\"M18 11V7a2 2 0 0 0-4 0v3-6a2 2 0 0 0-4 0v6-4a2 2 0 0 0-4 0v10l-1.6-1.6a2 2 0 0 0-2.8 2.8L5 21a4 4 0 0 0 3 1h8a6 6 0 0 0 6-6v-5a2 2 0 0 0-4 0v2\" />",
    "clock": "<circle cx=\"12\" cy=\"12\" r=\"10\" /> <path d=\"M12 6v6l4 2\" />",
    "chevron-down": "<path d=\"m6 9 6 6 6-6\" />",
    "chevron-right": "<path d=\"m9 18 6-6-6-6\" />",
    "pencil": "<path d=\"M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z\" /> <path d=\"m15 5 4 4\" />",
    "skip-forward": "<path d=\"M21 4v16\" /> <path d=\"M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z\" />",
    "palette": "<path d=\"M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z\" /> <circle cx=\"13.5\" cy=\"6.5\" r=\".5\" fill=\"currentColor\" /> <circle cx=\"17.5\" cy=\"10.5\" r=\".5\" fill=\"currentColor\" /> <circle cx=\"6.5\" cy=\"12.5\" r=\".5\" fill=\"currentColor\" /> <circle cx=\"8.5\" cy=\"7.5\" r=\".5\" fill=\"currentColor\" />",
    "file-text": "<path d=\"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z\" /> <path d=\"M14 2v5a1 1 0 0 0 1 1h5\" /> <path d=\"M10 9H8\" /> <path d=\"M16 13H8\" /> <path d=\"M16 17H8\" />",
    "circle-check": "<circle cx=\"12\" cy=\"12\" r=\"10\" /> <path d=\"m16 9-5.5 5.5L8 12\" />",
    "at-sign": "<circle cx=\"12\" cy=\"12\" r=\"4\" /> <path d=\"M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8\" />",
    "corner-down-right": "<path d=\"m15 10 5 5-5 5\" /> <path d=\"M4 4v7a4 4 0 0 0 4 4h12\" />",
    "bell": "<path d=\"M10.268 21a2 2 0 0 0 3.464 0\" /> <path d=\"M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326\" />",
  };
  function icon(name, cls) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('data-icon', name);
    s.setAttribute('class', 'ico' + (cls ? ' ' + cls : ''));
    s.setAttribute('stroke-width', '1.5');
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
  function newPinId() { return 'p' + String(uuid()).replace(/-/g, '').slice(0, 8); }
  function pct(v) { return Math.round(v * 100) + '%'; }
  function num(v, lo, hi) { v = +v; return isFinite(v) ? Math.min(Math.max(v, lo), hi) : lo; }
  function scrollPoint() {
    return { x: (typeof window !== 'undefined' && (window.pageXOffset || window.scrollX)) || 0,
             y: (typeof window !== 'undefined' && (window.pageYOffset || window.scrollY)) || 0 };
  }
  function docPoint(v) {   // a bounded page (document) point: finite px only
    if (!v || typeof v !== 'object' || typeof v.x !== 'number' || typeof v.y !== 'number') return null;
    if (!isFinite(v.x) || !isFinite(v.y)) return null;
    return { x: num(v.x, 0, 1000000), y: num(v.y, 0, 1000000) };
  }
  function letter(id) { return id.toUpperCase(); }

  // ---------- state ----------
  function R() { return S.review; }
  function roundObj(n) { var r = R().rounds; for (var i = 0; i < r.length; i++) if (r[i].n === n) return r[i]; return r[r.length - 1]; }
  function cur() { return roundObj(R().current_round); }
  function viewed() { return roundObj(S.view); }
  function key(suffix, n) { return 'mr:' + R().id + ':r' + (n || S.view) + ':' + suffix; }
  function localSent(n) { return lsGet(key('sent', n)); }
  // A wake-off board saves feedback without pinging Hermes, and so does a Send the request gate held (the server's
  // `detail` says why): one copy for the banner, the send bar and the toast.
  var STORED_GUEST_COPY = 'Saved by a share-link guest. Hermes was not pinged: tell it in chat.';
  var OWNER_ONLY_COPY = 'Only the owner sends this round to Hermes. Your comments and pins are on the board for them.';
  function savedCopy(state, otherwise, sub) {
    if (state && state.wake === false) return 'Saved. This board does not ping Hermes: tell it in chat.';
    if (!ownerSends()) return 'Saved on the board. Hermes was not pinged: only the owner sends a round to Hermes.';
    return (sub && sub.detail) || otherwise;
  }

  function submittedFeedback(r) {
    if (r.feedback) return r.feedback;
    var ls = r.n === R().current_round ? localSent(r.n) : null;
    return ls ? ls.feedback : null;
  }
  function isEditable() {
    var r = viewed();
    if (R().status !== 'open' || r.n !== R().current_round || !canDo('submit')) return false;
    return !submittedFeedback(r) || S.amending;
  }
  function isNew(r) { r = r || viewed(); return r.kind === 'decisions' || r.kind === 'prototype'; }
  function emptyDraft(r) {
    if (isNew(r)) return { marks: {}, pins: [], page_state: {}, page_marks: {}, page_notes: {}, guidelines: '', idem: uuid() };
    var d = { choice: null, choice_note: '', guidelines: '', variants: {}, idem: uuid() };
    r.variants.forEach(function (v) { d.variants[v.id] = { note: '', pins: [] }; });
    return d;
  }
  function put(o, k, v) {
    Object.defineProperty(o, k, { value: v, writable: true, enumerable: true, configurable: true });
  }
  function cleanPageState(v) {
    var out = {};
    if (!v || typeof v !== 'object' || Array.isArray(v)) return out;
    Object.keys(v).forEach(function (id) {
      var m = v[id];
      if (!m || typeof m !== 'object' || Array.isArray(m)) return;
      var block = {};
      Object.keys(m).forEach(function (k) {
        try { var s = JSON.stringify(m[k]); if (typeof s === 'string') put(block, k, JSON.parse(s)); } catch (e) {}
      });
      if (Object.keys(block).length) put(out, id, block);
    });
    return out;
  }
  function normalize(fb, r) {
    var d = emptyDraft(r);
    if (!fb) return d;
    if (isNew(r)) {
      Object.keys(fb.marks || {}).forEach(function (k) {
        var m = fb.marks[k] || {};
        d.marks[k] = { status: m.status || null, option: m.option || null, note: m.note || '' };
        if (m.choice) { d.marks[k].choice = m.choice; d.marks[k].custom = m.custom || ''; }   // a question's answer
      });
      d.pins = (fb.pins || []).map(function (p) {
        var t = p.target && cleanTarget(p.target);
        var doc = p.on === 'board' ? docPoint(p.doc) : null;   // a targetless board pin's page spot
        return { ...(p.id ? {id: p.id} : {}), block: p.block, option: p.option || null, w: p.w, x: p.x, y: p.y, text: p.text,
          ...(p.on === 'board' ? {on: 'board'} : {}), ...(p.thread ? {thread: p.thread} : {}), ...(doc ? {doc: doc} : {}), ...(t ? {target: t} : {}) };
      });
      d.page_state = cleanPageState(fb.page_state);
      d.page_marks = {};
      Object.keys(fb.page_marks || {}).forEach(function (k) { if (fb.page_marks[k]) put(d.page_marks, k, true); });
      d.page_notes = {};
      Object.keys(fb.page_notes || {}).forEach(function (k) { if (fb.page_notes[k]) put(d.page_notes, k, true); });
      d.guidelines = fb.guidelines || '';
      return d;
    }
    d.choice = fb.choice || null;
    d.choice_note = fb.choice_note || '';
    d.guidelines = fb.guidelines || '';
    r.variants.forEach(function (v) {
      var x = (fb.variants || {})[v.id] || {};
      d.variants[v.id] = { note: x.note || '', pins: (x.pins || []).map(function (p) { return { frame: p.frame, x: p.x, y: p.y, page_h: p.page_h, text: p.text, ...(p.thread ? {thread: p.thread} : {}) }; }) };
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
    if (d.marks) return (d.guidelines || '').trim() !== '' || d.pins.length > 0 || Object.keys(d.page_state || {}).length > 0 || Object.keys(d.marks).some(function (k) { var m = d.marks[k]; return m.status || m.option || m.choice || (m.note || '').trim(); });
    if (d.choice || d.guidelines.trim() || d.choice_note.trim()) return true;
    return Object.keys(d.variants).some(function (k) { return d.variants[k].note.trim() || d.variants[k].pins.length; });
  }
  function payload(d, action) {
    if (d.marks) return Object.assign(action ? { action: action } : {}, { marks: cleanMarks(d.marks), pins: d.pins.map(function (p) {
      var t = p.target && cleanTarget(p.target);
      var doc = p.on === 'board' ? docPoint(p.doc) : null;
      return { ...(p.id ? {id: p.id} : {}), block: p.block, option: p.option, w: p.w, x: +(+p.x).toFixed(4), y: +(+p.y).toFixed(4), text: p.text,
        ...(p.on === 'board' ? {on: 'board'} : {}), ...(doc ? {doc: doc} : {}), ...(t ? {target: t} : {}) };
    }), page_state: d.page_state || {}, guidelines: d.guidelines });
    var out = { choice: d.choice, choice_note: d.choice_note, guidelines: d.guidelines, variants: {} };
    Object.keys(d.variants).forEach(function (k) {
      out.variants[k] = { note: d.variants[k].note, pins: d.variants[k].pins.map(function (p) {
        return { frame: p.frame, x: +p.x.toFixed(4), y: +p.y.toFixed(4), page_h: Math.round(p.page_h || 0), text: p.text };
      }) };
    });
    return out;
  }

  // ---------- format 1.2: question marks, the gate and the progress line ----------
  // Pure functions (no DOM, no state) so the tests run the real code. They mirror feedback.py: a question is resolved
  // by a choice, any other markable block by approve or skip, and the gate opens once nothing is open or on Change.
  function entryKind(e) { return e.kind || (e.layout === 'recap' ? 'recap' : e.layout === 'prototype' ? 'prototype' : 'decision'); }
  function capCustom(v) { return String(v === null || v === undefined ? '' : v).replace(/[\r\n\t]+/g, ' ').slice(0, LIMIT.custom); }
  function isResolved(e, m) {
    m = m || {};
    if (entryKind(e) === 'question') return m.choice === 'recommended' || (m.choice === 'custom' && !!String(m.custom || '').trim());
    return m.status === 'approve' || m.status === 'skip';
  }
  function gateState(index, marks, gate) {
    var open = [], changed = [], bits = [];
    (index || []).forEach(function (e) {
      var m = (marks || {})[e.id] || {};
      if (entryKind(e) !== 'question' && m.status === 'change') changed.push(e.id);
      else if (!isResolved(e, m)) open.push(e.id);
    });
    if (open.length) bits.push(open.length + ' still open');
    if (changed.length) bits.push(changed.length + ' on Change');
    return { has_gate: !!gate, open: open, changed: changed, ready: !!gate && !bits.length, reason: bits.join(' · ') };
  }
  var PROGRESS = [['plan', 'plan'], ['question', 'questions'], ['pick', 'picks'], ['recap', 'to confirm']];
  function progressLine(index, marks) {
    var c = {};
    (index || []).forEach(function (e) {
      var k = entryKind(e), g = k === 'question' ? 'question' : k === 'decision' ? 'pick' : k === 'recap' ? 'recap' : k === 'prototype' ? null : 'plan';
      if (!g) return;
      c[g] = c[g] || [0, 0];
      c[g][1]++;
      if (isResolved(e, (marks || {})[e.id])) c[g][0]++;
    });
    return PROGRESS.filter(function (p) { return c[p[0]]; }).map(function (p) { return c[p[0]][0] + '/' + c[p[0]][1] + ' ' + p[1]; }).join(' · ');
  }
  function isMarkableBlock(b) {
    return b.type === 'decision' || b.type === 'question' || b.type === 'piece' || b.type === 'defaults' || (b.type === 'recap' && b.markable !== false) ||
      (b.type === 'artifact' && !!b.markable);
  }
  // Plan preset: markable blocks grouped by `group` (first appearance first); a context block joins the group of the
  // markable block before it, or `lead` when none came yet. settled / defaults are hoisted above the body by the caller.
  function groupBlocks(blocks) {
    var lead = [], groups = [], by = {}, last = null;
    blocks.forEach(function (b) {
      if (b.type === 'settled' || b.type === 'defaults') return;
      if (isMarkableBlock(b)) {
        var g = b.group || '';
        if (!by[g]) { by[g] = { name: g, blocks: [] }; groups.push(by[g]); }
        last = by[g];
        last.blocks.push(b);
      } else (last ? last.blocks : lead).push(b);
    });
    return { lead: lead, groups: groups };
  }
  // "Approve the rest": every still-open (no status yet) block of the group that takes approve/change/skip.
  function approveRestIds(index, group, marks) {
    return (index || []).filter(function (e) {
      var k = entryKind(e);
      return (e.group || '') === group && k !== 'question' && k !== 'defaults' && k !== 'prototype' && !((marks || {})[e.id] || {}).status;
    }).map(function (e) { return e.id; });
  }
  // The group has a block "Approve the rest" can act on (same kinds as approveRestIds); a question-only group gets no button.
  function groupHasBulk(index, group) {
    return (index || []).some(function (e) {
      var k = entryKind(e);
      return (e.group || '') === group && k !== 'question' && k !== 'defaults' && k !== 'prototype';
    });
  }
  // The gate button shows a lock only while it is locked; ready (and still editable, so also while sending) is `send`.
  function gateIcon(ready, editable) { return ready && editable ? 'send' : 'lock'; }
  // The summary line above the end of the board. A gate round never repeats the gate card's own call to action.
  function endLine(index, marks, gate, next, editable) {
    var idx = index || [], m = marks || {}, total = idx.length, nx = next || '', open = 0;
    if (gate) {
      var gs = gateState(idx, m, gate);
      if (gs.ready) return 'All ' + total + ' decided.' + (nx ? ' Next → ' + nx : '');
      var head = gs.open.length ? gs.open.length + ' of ' + total + ' still open' + (gs.changed.length ? ' · ' + gs.changed.length + ' on Change' : '') + '.'
        : gs.changed.length + ' of ' + total + ' on Change.';
      return head + (nx ? ' Once all are decided → ' + nx : '');
    }
    idx.forEach(function (d) {
      var mk = m[d.id] || {}, s = entryKind(d) === 'question' ? (isResolved(d, mk) ? 'approve' : null) : mk.status;   // an answered question counts as approved
      if (s !== 'approve' && s !== 'change' && s !== 'skip') open++;
    });
    if (open) return open + ' of ' + total + ' still open.' + (nx ? ' Once all are decided → ' + nx : '');
    return 'All ' + total + ' decided.' + (nx ? ' Next → ' + nx : '') + (editable ? ' Press Send when you’re ready.' : '');
  }
  // A question mark that is a custom answer with no text is not an answer: send the note alone, or nothing.
  function cleanMarks(marks) {
    var out = {};
    Object.keys(marks || {}).forEach(function (k) {
      var m = Object.assign({}, marks[k]);
      if (m.choice === 'custom') { m.custom = capCustom(m.custom).replace(/\s+/g, ' ').trim(); if (!m.custom) { m.choice = null; delete m.custom; } }
      else if (m.choice === 'recommended') delete m.custom;
      if (m.choice || m.status || m.option || (m.note || '').trim()) out[k] = m;
    });
    return out;
  }
  // ---------- boot ----------
  function applyTheme() {
    var t = R() ? boardTheme() : 'auto';
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
  }
  
  function applyLayout() {
    document.body.classList.remove('layout-inline', 'layout-cards');
    document.body.classList.add('layout-' + (CFG.layout === 'cards' ? 'cards' : 'inline'));
  }

  applyTheme();

  var serverClockOffset = null;
  function serverNow() {
    return serverClockOffset === null ? Date.now() : serverClockOffset + performance.now();
  }

  function fetchState() {
    return fetch('state.json', { cache: 'no-store', credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw Object.assign(new Error('state.json → HTTP ' + res.status), {status: res.status});
      return res.json();
    }).then(function (st) {
      serverClockOffset = Number.isFinite(st.now) ? st.now * 1000 - performance.now() : null;
      return st;
    });
  }

  // Every state replacement goes through here: the tokens are part of the state, so a round that arrives while the
  // board is open must never keep the old map (its frame URL would lose `~<token>` and reload unauthenticated).
  function applyState(st) {
    S.review = st;
    S.frameTokens = st.frame_tokens || S.frameTokens;
  }

  function boot() {
    fetchState().then(function (st) {
      applyState(st);
      applyPinSnapshot(st);
      loadCfg(st);
      S.width = initialWidth();
      var want = parseInt((location.hash.match(/round-(\d+)/) || [])[1], 10);
      S.view = (want && st.rounds.some(function (r) { return r.n === want; })) ? want : st.current_round;
      document.title = st.title + ' · Mock review';
      loadDraft();
      render();
      T.boot(st.me);
      T.pinActivityChanged();
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
    var keys = [], sec = closestOf(el, '[id]');
    var opt = closestOf(el, 'figure.opt[data-o]');
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
    var r = rectOf(el); if (!r) return false;
    var line = readingLine();
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
    closeExportMenu();
    if (CANVAS_LIVE) { CANVAS_LIVE.dispose(); CANVAS_LIVE = null; }
    disposeSticky();
    MOCKS.forEach(function (m) { if (m.anchorSurface) m.anchorSurface.dispose(); if (m.ro) m.ro.disconnect(); });
    S.frames = [];
    app.textContent = '';
    document.body.classList.toggle('tool-comment', S.commentMode);
    document.body.classList.toggle('tool-interact', !S.commentMode);
    document.body.classList.toggle('is-readonly', !isEditable());
    var r = viewed();
    applyDesign(r);
    document.body.classList.toggle('dz-mode', isNew(r));
    document.body.classList.toggle('page-mode', isNew(r) && r.kind === 'prototype');
    document.body.classList.toggle('mock-use', isNew(r) && !S.commentMode);
    if (isNew(r)) { renderNew(r); return; }
    append(app, [renderHeader(), renderToolbar(), renderStatusBanner(), renderPinnedBanner(), renderVariants(r), renderDecision(r), renderSendbar()]);
    requestAnimationFrame(layoutAll);
  }

  // A guest's name pill (they pick the name others see; a random one until then), or a Settings link for an account.
  var GUEST_WORDS = [['Blue', 'Green', 'Amber', 'Coral', 'Silver', 'Violet', 'Teal', 'Copper'],
                     ['Fox', 'Heron', 'Otter', 'Lynx', 'Wren', 'Badger', 'Falcon', 'Hare']];
  function randomGuestName() {
    var pick = function (a) { return a[Math.floor(Math.random() * a.length)]; };
    return pick(GUEST_WORDS[0]) + ' ' + pick(GUEST_WORDS[1]);
  }
  function initials(name) {
    return (String(name || '').trim().split(/\s+/).map(function (w) { return w.charAt(0); }).join('').slice(0, 2) || '?').toUpperCase();
  }
  var guestNameTimer = null;
  function saveGuestName(name) {
    lsSet('mr:guest-name', name);
    clearTimeout(guestNameTimer);
    guestNameTimer = setTimeout(function () {
      postJSON('api/guest-name', { name: name }).then(function (res) {
        if (res.ok && S.review.me) S.review.me.name = name;
      }, function () {});
    }, 500);
  }
  function guestDisplayName() {
    var m = me();
    if (!m) return '';
    if (m.kind !== 'guest') return m.name || 'Account';
    var name = lsGet('mr:guest-name') || m.name || randomGuestName();
    if (name !== m.name) saveGuestName(name);
    return name;
  }
  function avatarControl() {
    var m = me();
    if (!m) return null;
    var name = guestDisplayName();
    var av = h('span', { class: 'av av-self', 'aria-hidden': 'true', title: name + (m.kind === 'guest' ? ' · guest' : ''), text: initials(name) });
    paintAvatar(av, m.actor || (m.user_id ? 'user:' + m.user_id : (m.guest_id ? 'guest:' + m.guest_id : '')), name);
    return h('span', { class: 'who' }, av);
  }
  function guestNameEditor() {
    var m = me();
    if (!m || m.kind !== 'guest') return null;
    var name = guestDisplayName();
    var input = h('input', { class: 'menu-input', value: name, maxlength: '40', 'aria-label': 'Your name', spellcheck: 'false' });
    input.addEventListener('input', function () {
      var v = input.value.replace(/\s+/g, ' ').trim();
      var av = document.querySelector('.av-self');
      if (av && v) {
        av.textContent = initials(v);
        av.title = v + ' · guest';
        paintAvatar(av, m.actor || (m.guest_id ? 'guest:' + m.guest_id : ''), v);
      }
      if (v) saveGuestName(v);
    });
    return h('label', { class: 'menu-field' }, h('span', { class: 'menu-field-label', text: 'Your name' }), input);
  }
  function settingsMenuItem() {
    var m = me();
    if (!m || m.kind === 'guest') return null;
    return h('a', { class: 'menu-item', href: '/settings', role: 'menuitem' },
      icon('settings'), h('span', { text: 'Settings' }));
  }
  function signOutMenuItem() {
    var m = me();
    if (!m || m.source === 'loopback') return null;
    return h('button', { class: 'menu-item', type: 'button', role: 'menuitem',
      onclick: function () { closeExportMenu(); signOutNow(); } }, icon('signout'), h('span', { text: 'Sign out' }));
  }
  function signOutNow() {
    fetch('/api/logout', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() }, body: '{}' }).then(function () {
      location.href = '/';
    }, function () { toast('Could not sign out', 'warn'); });
  }

  var MODE_LABELS = { tailscale: 'Tailnet', local: 'Local', public: 'Public' };

  function modeBadge(st, compact) {
    var mode = st.mode === 'public' ? 'public' : st.mode === 'local' ? 'local' : 'tailscale';
    var title = mode === 'public'
      ? 'A public URL behind Tailscale Funnel or any TLS proxy'
      : mode === 'local'
        ? 'Served from this computer at its local address, no Tailscale'
        : 'Served from the board server, reachable on the tailnet only';
    var iconName = mode === 'public' ? 'globe' : mode === 'local' ? 'home' : 'lock';
    var label = compact ? h('span', { class: 'bl', text: MODE_LABELS[mode] }) : MODE_LABELS[mode];
    return h('span', { class: 'badge ' + (mode === 'public' ? 'badge-public' : 'badge-private'), title: title },
      icon(iconName), label);
  }

  function brandMark() {
    // The real mark, from the one file that carries its path data (the board's own favicon).
    return h('img', { class: 'brand-mark logo', src: '/_/favicon.svg', alt: '', 'aria-hidden': 'true' });
  }
  function shareButton() {
    return canDo('manage') ? h('button', {
      class: 'btn btn-outline share-open', type: 'button', 'aria-haspopup': 'dialog',
      title: 'Invite people and share this review', onclick: openShare
    }, icon('share'), h('span', { class: 'lb', text: 'Share' })) : null;
  }
  function renderHeader() {
    var st = R(), v = viewed();
    var brief = st.brief ? h('p', { class: 'brief', text: st.brief }) : null;
    if (brief) {
      brief.addEventListener('click', function () { brief.classList.toggle('open'); });
      brief.title = 'Tap to expand';
    }
    return h('header', { class: 'top' },
      h('div', { class: 'top-row' },
        h('div', { class: 'brand' }, brandMark(), h('span', { class: 'brand-name', text: 'Mock review' })),
        h('div', { class: 'top-actions' }, T.workingCount(), shareButton(), avatarControl(), moreButton()),
        h('span', { class: 'header-tools', 'aria-hidden': 'true' }, T.headerActions())),
      h('div', { class: 'title-wrap' },
        h('h1', { class: 'title', title: st.title, text: st.title }),
        h('p', { class: 'title-sub', text: 'Round ' + v.n + ' · ' + st.rounds.length + ' round' + (st.rounds.length === 1 ? '' : 's') })),
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
  function segSync(el, value) {
    Array.prototype.forEach.call(el.querySelectorAll('.seg-btn'), function (b, i) {
      var on = b.getAttribute('data-value') === String(value);
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  function segWithValues(options, value, onpick, label) {
    var el = seg(options, value, onpick, label);
    Array.prototype.forEach.call(el.querySelectorAll('.seg-btn'), function (b, i) {
      b.setAttribute('data-value', String(options[i].value));
    });
    return el;
  }

  // One toggle for every surface: decisions, prototype, full-screen and legacy variants (decided 2026-11-12).
  function commentToggle(mobile) {
    if (!canComment()) return null;
    var on = !!S.commentMode;
    return h('button', {
      class: 'ctoggle ' + (mobile ? 'foot-comment' : 'bar-comment'), id: mobile ? 'comment-toggle-mobile' : 'comment-toggle',
      'data-comment-toggle': '', type: 'button',
      'aria-pressed': on ? 'true' : 'false',
      title: on ? 'Exit comment mode (C)' : 'Comment (C)',
      'aria-label': on ? 'Exit comment mode (C)' : 'Comment (C)',
      onclick: function (e) { e.preventDefault(); setCommentMode(!S.commentMode); }
    }, icon('pin'), h('span', { class: 'lb', text: on ? 'Exit comment mode' : 'Comment' }));
  }

  // The pinned hint under the toolbar or the decision header; syncComment() rewrites it in place.
  function hintText(on) {
    return on
      ? 'Click anything to pin a note, mock buttons and links too. Pick, Approve, Change, Skip and notes still work. Leave comment mode (Esc or the Comment button) to use a mock’s controls.'
      : 'Press Comment (C) to pin a note on anything.';
  }
  function commentHint(cls) {
    var on = !!S.commentMode;
    var long = hintText(on);
    return h('p', { class: cls, 'data-comment-hint': '' }, icon(on ? 'hand' : 'pin'),
      h('span', { class: 'hint-long', text: long }), h('span', { class: 'hint-short', text: long }));
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
    var r = viewed();
    var jumps = h('div', { class: 'jumps' }, r.variants.map(function (v) {
      return h('a', { class: 'jump', href: '#variant-' + v.id, text: letter(v.id),
        onclick: function (e) { e.preventDefault(); var t = document.getElementById('variant-' + v.id); if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
    }));
    var hint = canComment() ? commentHint('hint') : null;
    return h('div', { class: 'toolbar-wrap', id: 'toolbar' },
      h('div', { class: 'toolbar' }, rounds, h('div', { class: 'tb-right' }, devices, commentToggle())),
      h('div', { class: 'toolbar-sub' }, hint, jumps));
  }

  function renderStatusBanner() {
    var st = R(), r = viewed();
    if (st.status !== 'open') {
      return h('div', { class: 'banner banner-muted' }, 'This review is closed. Everything below is read-only.');
    }
    if (!canDo('submit')) {
      return h('div', { class: 'banner banner-muted' },
        'You can look at this review' + (canDo('comment') ? ' and comment on it' : '') + '. Only the owner sends feedback to Hermes.');
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
      if (R().wake === false || (r.submission && r.submission.status === 'stored') || (!ownerSends() && !r.submission)) {
        return h('div', { class: 'banner banner-ok', role: 'status' },
          h('span', { text: savedCopy(R(), STORED_GUEST_COPY, r.submission) }));
      }
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

  function renderPinnedBanner() {
    if (!R() || !R().pinned_round) return null;
    return h('div', { class: 'banner banner-muted' },
      h('span', { text: 'This link shows round ' + R().pinned_round + '.' }));
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
    var pins = S.draft.variants[vid].pins.filter(pinVisible);
    var threads = [], seenThreads = {};
    if (S.commentMode) ['desktop', 'tablet', 'phone'].forEach(function (d) {
      T.frameEntries(vid, d).forEach(function (t) { if (!seenThreads[t.id]) { seenThreads[t.id] = 1; threads.push(t); } });
    });
    if (!pins.length && !threads.length) {
      list.appendChild(h('li', { class: 'pin-empty', text: isEditable() ? 'No pins yet — tap the mock to add one.' : 'No pins.' }));
      return;
    }
    pins.forEach(function (p, i) {
      list.appendChild(h('li', { class: 'pin-item' },
        h('button', { class: 'pin-row', onclick: function () { if (p.thread) T.open(p.thread, {detached: p.detached}); else focusPin(vid, i); } },
          h('span', { class: 'pin-num', text: String(pinNo(p, i)) }),
          h('span', { class: 'pin-body' },
            h('span', { class: 'pin-where', text: DEVICES[p.frame].label + ' · ' + pct(p.x) + ', ' + pct(p.y) }),
            h('span', { class: 'pin-text', text: p.text })))));
    });
    threads.forEach(function (t) { list.appendChild(T.row(t)); });
  }

  function autoGrow(ta) {
    if (ta.closest && ta.closest('.proto-dock')) { ta.style.height = ''; return; }   // the docked note is a fixed 40 px row
    ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 2, 320) + 'px';
  }

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
      src: frameUrl(r, v.file), title: 'Variant ' + letter(v.id) + ' — ' + v.label + ' (' + D.label + ')',
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
    if (f.ctx === 'proto' || f.ctx === 'artifact') return;
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
      if (p.frame === f.device && pinVisible(p)) out.push({ i: i, n: pinNo(p, i), x: p.x, y: p.y });
    });
    if (f.ghost) out.push({ i: -1, n: S.draft.variants[f.vid].pins.length + 1, x: f.ghost.x, y: f.ghost.y, ghost: true });
    if (S.commentMode) T.frameEntries(f.vid, f.device).forEach(function (t, j) {
      out.push({ i: 60 + j, n: t.n, x: t.anchor.x || 0, y: t.anchor.y || 0, detached: !!t.detached,
        ...(t.anchor.target ? { target: cleanTarget(t.anchor.target) } : {}), thread: t.id });
    });
    return out;
  }
  function pushState(f) {
    try {
      if (f.ctx === 'proto') return pushProto(f);
      if (f.ctx === 'artifact') return pushArtifact(f);
      f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'state', comment: canComment() && S.commentMode, scale: f.scale || 1, pins: framePins(f) }, '*');
    } catch (e) {}
  }
  function pushAll(vid) { S.frames.forEach(function (f) { if (!vid || f.vid === vid) pushState(f); }); }

  function frameFor(win) {
    for (var i = 0; i < S.frames.length; i++) if (S.frames[i].iframe.contentWindow === win) return S.frames[i];
    return null;
  }
  // A frame is untrusted HTML even when the board is showing its own mock: a message from it may ask for the thread
  // under the marker it drew, and only for a thread the board already lays out for that frame (the list that frame was
  // sent: same block, and the same device where the frame has one). That open never marks the thread read - read is a
  // trusted tap on the board itself (a pin row, a marker the board drew, a thread badge) - because a frame can post
  // any message it likes at any moment.
  function frameThreadEntries(f, block) {
    if (f.ctx === 'proto') return T.protoEntries();
    if (f.ctx === 'artifact' && !f.canvas) return T.mockEntries(block, null);
    return T.frameEntries(block, f.device);
  }
  function openFrameThread(f, block, id, detached, box) {
    if (typeof id !== 'string' || !id || id.indexOf('tmp:') === 0) return;
    var own = frameThreadEntries(f, block).filter(function (t) { return t.id === id; })[0];
    if (!own) return;
    T.open(id, {detached: !!detached, read: false, box: box});
  }
  function anchorOf(f, cx, cy) {
    var r = f.iframe.getBoundingClientRect();
    return { x: r.left + num(cx, 0, f.W) * f.scale, y: r.top + num(cy, 0, 4000) * f.scale };
  }

  window.addEventListener('message', function (ev) {
    var d = ev.data;
    if (!d || d.__mockreview !== 1 || typeof d.type !== 'string') return;
    var f = frameFor(ev.source);
    if (!f) return;
    if (f.ctx === 'proto') return protoMessage(f, d);
    if (f.ctx === 'artifact') return artifactMessage(f, d);
    if (d.type === 'hello') {
      f.alive = true;
      f.pageH = Math.round(num(d.h, 100, 20000));
      pushState(f);
      if (f.pendingFocus !== undefined) {
        var fi = f.pendingFocus; f.pendingFocus = undefined;
        setTimeout(function () { try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'focus', i: fi }, '*'); } catch (e) {} }, 60);
      }
    } else if (d.type === 'escape') {
      if (pop) closePopover(); else setCommentMode(false);
    } else if (d.type === 'click') {
      if (!canComment() || !S.commentMode) return;
      f.pageH = Math.round(num(d.h, 100, 20000));
      openPinEditor({ vid: f.vid, frame: f.device, x: num(d.x, 0, 1), y: num(d.y, 0, 1), page_h: f.pageH, index: -1, anchor: anchorOf(f, d.cx, d.cy), f: f });
    } else if (d.type === 'pin') {
      var i = num(d.i, -1, 99) | 0;
      if (i >= 60) {
        var t = T.frameEntries(f.vid, f.device)[i - 60];
        if (t) openFrameThread(f, f.vid, t.id, t.detached);
        return;
      }
      var p = S.draft.variants[f.vid].pins[i];
      if (!p || p.frame !== f.device) return;
      if (p.thread) { openFrameThread(f, f.vid, p.thread, p.detached); return; }
      openPinEditor({ vid: f.vid, frame: p.frame, x: p.x, y: p.y, page_h: p.page_h, index: i, anchor: anchorOf(f, d.cx, d.cy), f: f });
    } else if (d.type === 'positions') {
      var tp = T.frameEntries(f.vid, f.device);
      (Array.isArray(d.pins) ? d.pins : []).forEach(function (q) {
        var t = q && q.i >= 60 ? tp[q.i - 60] : null;
        if (!t || !Number.isFinite(q.x) || !Number.isFinite(q.y)) return;
        t.anchor.x = num(q.x, 0, 1);
        t.anchor.y = num(q.y, 0, 1);
        t.detached = !!q.detached;
      });
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
      T.attachMentions(ta);
      body = ta;
    } else {
      body = h('p', { class: 'pop-text', text: pins[o.index] ? pins[o.index].text : '' });
    }
    function save() {
      var text = ta.value.replace(/\s+/g, ' ').trim();
      if (!text) { ta.focus(); ta.classList.add('shake'); setTimeout(function () { ta.classList.remove('shake'); }, 400); return; }
      if (isNew) {
        var pin = { frame: o.frame, x: o.x, y: o.y, page_h: Math.round(o.page_h), text: text.slice(0, LIMIT.pin) };
        startThread(Object.assign({ block: o.vid, w: o.frame }, pin), pins, text.slice(0, LIMIT.pin), ta);
      } else {
        pins[o.index].text = text.slice(0, LIMIT.pin);
        if (pins[o.index].thread) T.editThreadFirst(pins[o.index].thread, text.slice(0, LIMIT.pin)).catch(function () {});
      }
      saveDraft(); closePopover(); redrawVariantPins(o.vid);
    }
    function del() {
      var p = pins[o.index];
      if (p && p.thread) T.deleteThreadFirst(p.thread).catch(function () {});
      pins.splice(o.index, 1); saveDraft(); closePopover(); redrawVariantPins(o.vid);
    }
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
    if (e.key === 'Escape') {
      if (S.exportMenu) { closeExportMenu(); return; }
      if (S.share) { closeShare(); return; }
      if (pop) { closePopover(); return; }
      if (S.commentMode) { setCommentMode(false); return; }
      if (S.full) closeFull();
      return;
    }
    if ((e.key === 'c' || e.key === 'C') && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (typing(e.composedPath ? e.composedPath()[0] : e.target) || !commentAvailable()) return;
      e.preventDefault();
      setCommentMode(!S.commentMode);
    }
  });
  function typing(el) {
    return !!closestOf(el, 'input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  }
  function commentAvailable() { return canComment(); }
  function setCommentMode(on) {
    var want = !!on && commentAvailable();
    if (want) S.frames.forEach(function (f) { if (f.canvas && f.hand && f.setHand) f.setHand(false); });
    if (want === S.commentMode && document.querySelector('[data-comment-toggle]')) { syncComment(); return; }
    S.commentMode = want;
    closePopover();
    var r = viewed();
    document.body.classList.toggle('tool-comment', want);
    document.body.classList.toggle('tool-interact', !want);
    document.body.classList.toggle('mock-use', isNew(r) && !want);
    document.body.classList.toggle('proto-pin', want);
    Array.prototype.forEach.call(document.querySelectorAll('.mock-host'), function (el) { el.classList.toggle('use', !want); });
    MOCKS.forEach(function (m) { if (!want && m.anchorSurface) m.anchorSurface.clearHover(); });
    if (!want) hideCanvasHover();
    syncComment();
    scheduleCanvasPins();
    pushAll();                     // legacy + full-screen frames (pushState → pushProto for the prototype)
    redrawThreadSurfaces();        // inline mocks and the pin list show thread markers only in comment mode
  }
  function syncComment() {
    var on = !!S.commentMode;
    var long = hintText(on);
    Array.prototype.forEach.call(document.querySelectorAll('[data-comment-toggle]'), function (b) {
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.setAttribute('title', on ? 'Exit comment mode (C)' : 'Comment (C)');
      b.setAttribute('aria-label', on ? 'Exit comment mode (C)' : 'Comment (C)');
      var lb = b.querySelector('.lb'); if (lb) lb.textContent = on ? 'Exit comment mode' : 'Comment';
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-comment-hint]'), function (p) {
      p.textContent = '';
      p.appendChild(icon(on ? 'hand' : 'pin'));
      p.appendChild(h('span', { class: 'hint-long', text: long }));
      p.appendChild(h('span', { class: 'hint-short', text: long }));
    });
    var close = document.querySelector('.modal [aria-label="Close"]');
    if (close) close.title = on ? 'Close' : 'Close (Esc)';
  }
  document.addEventListener('pointerdown', function (e) {
    if (pop && !pop.el.contains(e.target) && !closestOf(e.target, '.pin-row')) closePopover();
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
    var el = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Variant ' + letter(vid) + ' full size' },
      h('div', { class: 'modal-bar' },
        h('div', { class: 'modal-title' }, h('span', { class: 'v-letter', text: letter(vid) }), h('strong', { text: v.label })),
        h('div', { class: 'modal-tools' },
          seg([{ value: 'desktop', label: 'Desktop' }, { value: 'phone', label: 'Phone' }], device, function (d) { openFull(vid, d); }, 'Device'),
          commentToggle(),
          h('a', { class: 'icon-btn', href: frameUrl(r, v.file), target: '_blank', rel: 'noopener noreferrer', title: 'Open in a new tab', 'aria-label': 'Open in a new tab' }, icon('ext')),
          h('button', { class: 'icon-btn', onclick: closeFull, title: S.commentMode ? 'Close' : 'Close (Esc)', 'aria-label': 'Close' }, icon('close')))),
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
  var assetRevs = {};       // round n -> {block id: rev} the cached decisions.json reflects (state.json's revisions)
  var MOCKS = [];           // live inline mocks on screen
  var renderSeq = 0;
  S.width = window.innerWidth < 760 ? 'phone' : 'desktop';   // Desktop / Mobile: defaults to the device in hand
  S.show = {};              // decision id -> option shown behind the A|B|C toggle
  // The approved token names the design profile may recolour. --pin is deliberately absent: the pin is the
  // mock-review brand indigo in every theme and under every project profile.
  var DESIGN_VARS = ['--b', '--s', '--s2', '--l', '--t', '--m', '--a', '--ai', '--as', '--ok', '--oks', '--wn',
    '--wns', '--err', '--errs', '--font'];

  function roundBase(r) { return 'rounds/' + r.n + '/a/'; }
  // Every URL that becomes a frame document (or is navigated from inside one) carries the round's signed token in
  // its path; relative links a prototype derives then inherit it. Board-origin fetches (design.json, fonts CSS, the
  // favicon) stay on today's URL. No token in state.json (an older server) degrades to today's URL.
  function frameUrl(r, file) {
    var m = /^rounds\/[0-9]+\/([a-z])\/(.*)$/.exec(String(file || ''));
    var variant = m ? m[1] : 'a', rel = m ? m[2] : String(file || '');
    var tok = (S.frameTokens || {})[String(r.n)];
    return 'rounds/' + r.n + '/' + (tok ? '~' + tok + '/' : '') + variant + '/' + rel;
  }
  function narrow() { return window.innerWidth < 760; }
  function mark(id) { return (S.draft.marks && S.draft.marks[id]) || { status: null, option: null, note: '' }; }
  function pageOwned(id) { return !!(S.draft.page_marks && S.draft.page_marks[id]); }
  function pageNoteOwned(id) { return !!(S.draft.page_notes && S.draft.page_notes[id]); }
  function setMark(id, patch, fromPage) {
    var writesNote = Object.prototype.hasOwnProperty.call(patch, 'note');
    var m = Object.assign({}, mark(id), patch);
    if (markEmpty(m)) delete S.draft.marks[id]; else S.draft.marks[id] = m;
    if (!S.draft.page_marks) S.draft.page_marks = {};
    if (fromPage) put(S.draft.page_marks, id, true); else delete S.draft.page_marks[id];
    if (!S.draft.marks[id]) delete S.draft.page_marks[id];
    if (writesNote) {
      if (!S.draft.page_notes) S.draft.page_notes = {};
      if (fromPage) put(S.draft.page_notes, id, true); else delete S.draft.page_notes[id];
    }
    if (!S.draft.marks[id] && S.draft.page_notes) delete S.draft.page_notes[id];
    saveDraft(); updateSendbar(); refreshBlock(id); updateTally();
    if (fromPage && writesNote) {   // show the note the page wrote, but never over the box the reviewer is typing in
      var el = markSurface(id), ta = el && el.querySelector('.dnote');
      if (ta && document.activeElement !== ta) { ta.value = mark(id).note || ''; autoGrow(ta); }
    }
  }
  function markEmpty(m) { return !m.status && !m.option && !m.choice && !(m.note || '').trim(); }
  function initials(label) {
    var w = String(label || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/);
    return ((w[0] || '?')[0] + (w.length > 1 ? w[1][0] : (w[0][1] || ''))).toUpperCase();
  }

  // ---- per-review settings (state.json → settings) ----
  var CFG_DEFAULT = { devices: ['desktop', 'mobile'], default_device: 'auto', default_theme: 'project', show_theme_toggle: true, layout: 'inline',
    mobile_width: 390, tablet_width: 820, pins_enabled: true, require_note_on_change: false, max_pins: 40, footer_button: 'build', interactive_elements: false, hide_resolved_pins: false };   // defaults for reviews stored before a setting existed
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
    applyLayout();
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
  // footer_button: the one button a round with a gate gets in the footer: send or build.
  function footerButton() { return CFG.footer_button === 'send' ? 'send' : 'build'; }
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
    root.style.removeProperty('--pin');
    root.style.removeProperty('--pin-ring');   // the brand pin can never be recoloured by a profile or an old render
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
    set('--b', k.bg); set('--s', k.surface); set('--s2', k.surface2 || k.surface);
    set('--l', k.line || soft(k.text, 14));
    set('--t', k.text); set('--m', k.muted || soft(k.text, 72));
    set('--a', k.accent); set('--ai', k.accentInk || k.bg); set('--as', soft(k.accent, 16));
    if (k.ok) { set('--ok', k.ok); set('--oks', soft(k.ok, 14)); }
    if (k.wn || k.warn) { set('--wn', k.wn || k.warn); set('--wns', soft(k.wn || k.warn, 15)); }
    if (k.err) { set('--err', k.err); set('--errs', soft(k.err, 13)); }
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
  // Keep real hit testing and native selection in both tools. In comment mode a tap on anything but a LIVE element pins.
  var MOCK_BASE = ':host{display:block}.cq{container:mock/inline-size;pointer-events:auto}' +
    '.cq,.cq *{-webkit-user-select:auto;user-select:auto}' +
    '.cq a[href]{pointer-events:auto;cursor:pointer}' +
    ':host(.use) .cq{pointer-events:auto}';
  // Comment mode (a host without .use): the pin cursor over the whole mock, buttons and links included, since a click on
  // any of them pins; LIVE elements keep their own cursor.
  var MOCK_PIN_CURSOR = ':host(:not(.use)) .cq,:host(:not(.use)) .cq *{cursor:crosshair!important}' +
    ':host(:not(.use)) .cq :is(' + LIVE + '),:host(:not(.use)) .cq :is(' + LIVE + ') *{cursor:auto!important}';
  function loadAssets(r) {
    if (ASSETS[r.n]) return ASSETS[r.n];
    assetRevs[r.n] = blockRevs(r);   // read before the file: a patch landing in between only makes the next sync swap again
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
      out.css = MOCK_BASE + MOCK_PIN_CURSOR + toContainer(out.css + '\n' + ((out.dec && out.dec.extra_css) || ''));
      try { out.sheet = new CSSStyleSheet(); out.sheet.replaceSync(out.css); } catch (e) { out.sheet = null; }
      return out;
    });
    ASSETS[r.n].catch(function () { delete ASSETS[r.n]; delete assetRevs[r.n]; });
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
    t.content.querySelectorAll('script,iframe,frame,object,embed,base,meta,link').forEach(function (n) {
      n.remove();
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
    // A second pass with DOMPurify (vendor/purify.min.js) under the same bans, so a gap in the pass above is not the only
    // line of defence. Skipped when the library is missing, which leaves the first pass alone, as it always was.
    // `form` and `contenteditable` stay: a mock is a UI, so its forms and editable regions are the markup being reviewed.
    // A form cannot leave the page from here: the board CSP has form-action 'none' and the capture-phase submit listener
    // on the mock's shadow root calls preventDefault before any navigation.
    return typeof DOMPurify !== 'undefined' && DOMPurify.isSupported
      ? DOMPurify.sanitize(t.content, { RETURN_DOM_FRAGMENT: true, ADD_ATTR: ['target', 'contenteditable'], FORBID_ATTR: ['srcdoc', 'ping'],
          FORBID_TAGS: ['script', 'iframe', 'frame', 'object', 'embed', 'base', 'meta', 'link'],
          // A mock's markup lives in a shadow root, where ids cannot shadow properties of window/document, so
          // DOMPurify's clobbering guard (which strips common ids such as id="submit") buys nothing and breaks real UI.
          SANITIZE_DOM: false })
      : t.content;
  }

  // ---- the bar: project icon · title · rounds | Desktop/Mobile · theme · Tailnet/Local/Public · Send ----
  // The bar shows the reviewed project's icon from the round's design profile (labelled by the design). A round with
  // no design icon, or whose icon file fails to load, shows the mock-review mark instead.
  function projectIcon(r) {
    var d = r.design;
    if (!d || !d.icon) return brandMark();
    var img = h('img', { class: 'proj-icon', src: roundBase(r) + 'design/' + d.icon, alt: (d.label || R().title) });
    img.addEventListener('error', function () { img.replaceWith(brandMark()); });
    return img;
  }
  // Sticky heights: one live observer per surface. A rerender builds a new bar/footer, so the previous observer is
  // disconnected before the new one starts and render() releases both when it tears the page down; otherwise every
  // rerender keeps the detached node observed.
  var barRO = null, footRO = null, roundsRO = null, bannersRO = null;
  function watchSticky(el, prop, prev) {
    if (prev) prev.disconnect();
    if (!window.ResizeObserver) return null;
    var ro = new ResizeObserver(function () { document.documentElement.style.setProperty(prop, el.offsetHeight + 'px'); });
    ro.observe(el);
    return ro;
  }
  function disposeSticky() {
    if (barRO) { barRO.disconnect(); barRO = null; }
    if (footRO) { footRO.disconnect(); footRO = null; }
    if (roundsRO) { roundsRO.disconnect(); roundsRO = null; }
    if (bannersRO) { bannersRO.disconnect(); bannersRO = null; }
  }
  // A page-mode artifact's full bleed must break out against the layout viewport, not `vw`: `100vw` includes a classic
  // scrollbar that the document column does not, so the frame overhangs the page by the scrollbar's width. One observer
  // for the page's lifetime keeps the property current, including a scrollbar that appears with no window resize.
  var pageW = null, pageWRO = null;
  function measurePageWidth() {
    var w = document.documentElement.clientWidth + 'px';
    if (w === pageW) return;
    pageW = w;
    document.documentElement.style.setProperty('--page-w', w);
  }
  measurePageWidth();
  if (window.ResizeObserver) {
    pageWRO = new ResizeObserver(measurePageWidth);
    pageWRO.observe(document.documentElement);
  }
  // header: project icon · title | device widths · theme · Tailnet/Local/Public · Share
  function renderBar(r) {
    var st = R(), ws = widths();
    var widthSeg = null;
    if (ws.length > 1) {
      widthSeg = segWithValues(ws.map(function (w) { return { value: w, label: wlabel(w), icon: WICON[w], title: wlabel(w) + ' width' }; }),
        S.width, function (v) { if (v !== S.width) { S.width = v; keepPlace(render); } }, 'Mock width');
      widthSeg.classList.add('wseg');
    }
    var idx = r.decisions || [];
    var subline = 'Round ' + r.n + (r.kind === 'prototype' ? ' · prototype'
      : idx.length ? ' · ' + idx.length + (idx.length === 1 ? ' decision' : ' decisions') : '');
    var bar = h('header', { class: 'dzbar', id: 'dzbar' },
      projectIcon(r),
      h('div', { class: 'pb-title' }, h('strong', { class: 'pb-name', title: st.title, text: st.title }), h('span', { class: 'pb-sub', text: subline })),
      h('span', { class: 'spacer' }),
      h('div', { class: 'bar-actions' }, T.workingCount(), commentToggle(), widthSeg, shareButton(), avatarControl(), moreButton()),
      h('span', { class: 'header-tools', 'aria-hidden': 'true' }, T.headerActions()));
    barRO = watchSticky(bar, '--barh', barRO);
    return bar;
  }

  function roundPicker(r) {
    var btn = h('button', {
      class: 'btn round-picker', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      title: 'Switch round', onclick: function (e) {
        e.preventDefault(); e.stopPropagation();
        if (S.exportMenu) { closeExportMenu(); return; }
        openMenuAt(this, function (menu) {
          menu.appendChild(h('div', { class: 'menu-label', text: 'Rounds' }));
          R().rounds.forEach(function (x) {
            var sent = !!submittedFeedback(x);
            menu.appendChild(h('button', {
              class: 'menu-item menu-round' + (x.n === S.view ? ' on' : ''), type: 'button', role: 'menuitem',
              onclick: function () { closeExportMenu(); if (x.n !== S.view) switchRound(x.n); }
            }, h('span', { class: 'menu-round-n', text: 'R' + x.n }),
              h('span', { class: 'menu-round-label', text: 'Round ' + x.n }),
              x.n === R().current_round && R().status === 'open' ? h('span', { class: 'dot-live' }) : null,
              sent ? h('span', { class: 'tick' }, icon('check')) : null,
              x.n === S.view ? icon('check') : null));
          });
        }, { label: 'Rounds' });
      }
    }, h('span', { text: 'R' + S.view }), icon('chevron-down'));
    return btn;
  }

  // footer: rounds · tally · Send. A message row appears above it for errors / missing notes.
  var footMsg = null;
  // A prototype round's one decide surface: the same approve/change/skip row and note, compact, docked in the footer.
  function protoDecide() {
    var b = { id: 'proto', type: 'recap', options: [] };
    var m = mark('proto'), row = decideRow(b, false);
    var status = h('span', { class: 'status vh', id: 'proto-status', text: statusText(m) });
    status.dataset.s = m.status || '';
    append(row, status);
    return row;
  }
  function renderFoot(r) {
    var st = R(), compact = window.innerWidth <= 600;
    var rounds = h('nav', { class: 'rounds', 'aria-label': 'Rounds' }, st.rounds.map(function (x) {
      var sent = !!submittedFeedback(x);
      return h('button', { class: 'round-tab' + (x.n === S.view ? ' on' : ''), title: 'Round ' + x.n + (x.kind === 'prototype' ? ' · prototype' : ''),
        onclick: function () { if (x.n !== S.view) switchRound(x.n); } },
        h('span', { text: 'R' + x.n }), x.n === st.current_round && !sent ? h('span', { class: 'dot-live' }) : null,
        sent ? h('span', { class: 'tick', title: 'Feedback sent' }, icon('check')) : null);
    }));
    var compactTally = h('span', { class: 'tally-compact', id: 'dz-tally-compact', 'aria-label': 'Decisions so far' },
      h('b', { id: 'c-done', text: '0' }), h('span', { text: ' of ' }), h('span', { id: 'c-total', text: '0' }), h('span', { text: ' done' }));
    footMsg = h('div', { class: 'ft-msg', id: 'ft-msg', role: 'alert' });
    footMsg.hidden = true;
    sendbar = h('div', { class: 'sendbar sendbar-inline' });
    var foot;
    if (isNew(r) && r.kind === 'prototype') {
      // Page mode: one docked bar, three zones — round tabs | decide + note | tally + Send. CSS wraps it to two rows
      // under 720 px; the frame's height is measured from this one bar.
      var dec = protoDecide();
      foot = h('footer', { class: 'dzfoot proto-dock', id: 'dzfoot' }, footMsg,
        h('div', { class: 'ft-row proto-row' }, compact ? commentToggle(true) : null, rounds, dec, sendbar));
    } else {
      foot = h('footer', { class: 'dzfoot', id: 'dzfoot' }, footMsg,
        h('div', { class: 'ft-row' },
          compact ? commentToggle(true) : null,
          compact ? roundPicker(r) : rounds,
          compact ? compactTally : renderTally(r),
          h('span', { class: 'spacer' }),
          sendbar));
    }
    updateSendbar();
    requestAnimationFrame(updateTally);
    footRO = watchSticky(foot, '--footh', footRO);
    return foot;
  }

  // The round strip scrolls sideways, so the round being viewed must be scrolled into it. Rect math, because the strip
  // is not positioned (offsetLeft would resolve against the body); never scrollIntoView, which can scroll the page.
  // The observer covers the first layout and every width change; the rAF after the footer is appended covers a round
  // switch whose strip width happens to stay the same.
  function syncRoundStrip() {
    var strip = document.querySelector('#dzfoot .rounds');
    var on = strip && strip.querySelector('.round-tab.on');
    if (!strip || !on || !strip.clientWidth) return;
    var s = strip.getBoundingClientRect(), d = on.getBoundingClientRect();
    if (d.right > s.right) strip.scrollLeft += Math.ceil(d.right - s.right);
    else if (d.left < s.left) strip.scrollLeft -= Math.ceil(s.left - d.left);
  }
  function watchRoundStrip() {
    if (roundsRO) { roundsRO.disconnect(); roundsRO = null; }
    if (!window.ResizeObserver) return;
    var strip = document.querySelector('#dzfoot .rounds');
    if (!strip) return;
    roundsRO = new ResizeObserver(syncRoundStrip);
    roundsRO.observe(strip);
  }

  function renderNew(r) {
    var seq = ++renderSeq;
    MOCKS = [];
    var main = h('main', { class: 'dz dz-' + r.kind }, h('div', { class: 'boot' }, h('div', { class: 'boot-dot' }), h('p', { text: 'Loading round ' + r.n + '…' })));
    // A canvas keeps the footer on screen, so whatever a banner adds to the top is measured and taken off the surface.
    var banners = h('div', { class: 'dz-banners' }, renderStatusBanner(), renderPinnedBanner());
    append(app, [renderBar(r), banners.childNodes.length ? banners : null, main, renderFoot(r)]);
    bannersRO = watchSticky(banners, '--bannerh', bannersRO);
    if (!banners.childNodes.length) document.documentElement.style.setProperty('--bannerh', '0px');
    watchRoundStrip();
    requestAnimationFrame(syncRoundStrip);
    loadAssets(r).then(function (a) {
      if (seq !== renderSeq) return;
      main.textContent = '';
      append(main, r.kind === 'prototype' ? renderProto(r) : renderDoc(r, a));

      paintHermesBlocks();
      lastNarrow = narrow(); lastCompact = window.innerWidth <= 600;
    }).catch(function (e) {
      if (seq !== renderSeq) return;
      main.textContent = '';
      append(main, h('div', { class: 'boot error' }, h('h1', { text: 'This round could not be loaded' }), h('p', { text: String(e.message || e) }),
        h('button', { class: 'btn', onclick: function () { delete ASSETS[r.n]; render(); }, text: 'Try again' })));
    });
  }
  var lastNarrow = narrow(), lastCompact = window.innerWidth <= 600, rzTimer = null;
  window.addEventListener('resize', function () {
    measurePageWidth();
    clearTimeout(rzTimer);
    scheduleCanvasPins();
    rzTimer = setTimeout(function () {
      var nowCompact = window.innerWidth <= 600;
      if (S.review && isNew() && (narrow() !== lastNarrow || nowCompact !== lastCompact) && !pop) keepPlace(render);
    }, 150);
  });

  // ---- the decision doc ----
  function paras(text, cls) {
    return String(text || '').split(/\n{2,}/).filter(function (x) { return x.trim(); }).map(function (x) { return h('p', { class: cls || null, text: x.trim() }); });
  }
  function fmtDay(iso) {
    var d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  // Format 1.2 boards (the file has a `preset`) share one skeleton: header + status line, "Your notes, answered",
  // "Already decided", the defaults card, the body (grouped on the plan preset), then the gate. Older files have no
  // preset and keep the header and the flat body they always had.
  var SECTION_TITLES = {text: 'Text', cards: 'Cards', callout: 'Callout', table: 'Table', settled: 'Already decided',
    recap: 'Recap', defaults: 'How I read the rest', question: 'Question', decision: 'Decision', piece: 'Piece',
    mock: 'Mock', artifact: 'Artifact'};
  function derivedSections(dec) {   // store.section_index, mirrored for a round stored without its sections
    var ids = {};
    (dec.blocks || []).forEach(function (b) { if (b && b.id) ids[b.id] = 1; });
    var n = 0;
    return (dec.blocks || []).map(function (b) {
      var ref = b && b.id;
      if (!ref) { do { n += 1; ref = 's' + n; } while (ids[ref]); }
      var type = (b && b.type) || 'text';
      return {ref: ref, type: type,
        title: (b && b.title) || SECTION_TITLES[type] || type.charAt(0).toUpperCase() + type.slice(1)};
    });
  }
  function renderDoc(r, a) {
    if (CANVAS_LIVE) { CANVAS_LIVE.dispose(); CANVAS_LIVE = null; }
    clearCanvasHighlights();
    var dec = a.dec, st = R(), v12 = !!dec.preset, plan = dec.preset === 'plan';
    SECTIONS = new Map();
    CANVAS = new Map();
    var secs = Array.isArray(r.sections) && r.sections.length === dec.blocks.length ? r.sections : derivedSections(dec);
    dec.blocks.forEach(function (b, i) { if (secs[i]) SECTIONS.set(b, secs[i]); });
    hideCanvasHover();
    var doc = h('div', { class: 'doc' + (isEditable() ? '' : ' ro') });
    doc.setAttribute('data-mr-ref', 'doc');
    doc.setAttribute('data-mr-title', 'Board canvas');
    var eyebrow = v12
      ? [(r.design && r.design.label) || st.title, dec.preset, 'Round ' + r.n, fmtDay(r.created_at)].filter(Boolean).join(' · ')
      : [(r.design && r.design.label), 'Round ' + r.n, wlabel(S.width) + ' mocks'].filter(Boolean).join(' · ');
    var head = h('header', { class: 'doc-head' },
      h('div', { class: 'eyebrow', text: eyebrow }),
      h('h1', { text: st.title }),
      paras(dec.intro || st.brief),
      v12 ? renderStatusLine(dec) : null,
      canComment() ? commentHint('hint-line') : null);
    head.setAttribute('data-mr-ref', 'page');
    head.setAttribute('data-mr-title', 'Board');
    var showIntro = dec.show_intro !== false, showOutro = dec.show_outro !== false;   // old rounds have neither key
    // When the artifact is the entire body, put it directly under the sticky bar. Keep the title and review rows below it.
    var loneCanvas = dec.blocks.length === 1 && dec.blocks[0].type === 'artifact' && dec.blocks[0].display === 'canvas';
    if (loneCanvas) {
      doc.classList.add('art-canvas-doc');
      append(doc, renderBlock(r, a, dec.blocks[0]));
    }
    if (showIntro) append(doc, head);
    var body = dec.blocks.filter(function (b) { return b.type !== 'settled' && b.type !== 'defaults'; });
    var grouped = plan ? groupBlocks(dec.blocks) : null;
    append(doc, renderDocNav(r, grouped));
    append(doc, renderNotesAnswered(dec));
    dec.blocks.forEach(function (b) { if (b.type === 'settled') append(doc, renderBlock(r, a, b)); });
    dec.blocks.forEach(function (b) { if (b.type === 'defaults') append(doc, renderBlock(r, a, b)); });
    if (!loneCanvas && grouped) {
      grouped.lead.forEach(function (b) { append(doc, renderBlock(r, a, b)); });
      grouped.groups.forEach(function (g, i) { append(doc, renderGroup(r, a, g, i)); });
    } else if (!loneCanvas) body.forEach(function (b) { append(doc, renderBlock(r, a, b)); });
    if (showOutro) {
      append(doc, renderElse());
      append(doc, h('div', { class: 'doc-end', id: 'dz-end' }));
    }
    if (dec.gate && footerButton() === 'build') append(doc, renderGate(r, dec));
    requestAnimationFrame(updateTally);
    DOC = { r: r, dec: dec, el: doc };
    stamp(doc);
    doc.addEventListener('click', canvasClick, true);
    doc.addEventListener('pointermove', canvasHover, true);
    doc.addEventListener('pointerleave', hideCanvasHover, true);
    CANVAS_LIVE = canvasLifecycle(doc);
    CANVAS_LIVE.schedule();
    return doc;
  }
  var DOC = null;
  var SECTIONS = new Map();   // block object -> {ref, type, title} for this render (empty on old rounds)
  var CANVAS = new Map();     // section element -> {layer, list}, created lazily per section
  var CANVAS_LIVE = null;     // the current doc's observer lifecycle, disposed on the next render

  // status.label, the milestones and the live progress line ("3/5 plan · 1/2 questions · 0/1 picks")
  function renderStatusLine(dec) {
    var s = dec.status || {}, ms = s.milestones || [];
    var progress = progressLine(viewed().decisions, S.draft.marks);
    if (!s.label && !ms.length && !progress) return null;
    return h('div', { class: 'statusline' },
      s.label ? h('p', { class: 'st-label', text: s.label }) : null,
      ms.length ? h('ul', { class: 'st-miles', 'aria-label': 'Milestones' }, ms.map(function (m) {
        return h('li', null, h('span', { text: m.label }), m.date ? h('span', { class: 'st-date', text: m.date }) : null);
      })) : null,
      h('p', { class: 'st-progress', id: 'dz-progress', text: progress }));
  }
  var NOTE_CHIP = { done: 'Done', 'second-look': 'Needs a second look', 'needs-pick': 'Needs a pick' };
  function jumpTo(id) {
    var el = document.getElementById(id); if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('flash-blk');
    setTimeout(function () { el.classList.remove('flash-blk'); }, 1400);
  }
  function renderNotesAnswered(dec) {
    var list = dec.notes_answered || [];
    if (!list.length) return null;
    return h('section', { class: 'blk blk-notes', id: 'dz-notes' }, h('h2', { text: 'Your notes, answered' }),
      h('ul', { class: 'na' }, list.map(function (n) {
        var ref = dec.blocks.filter(function (b) { return b.id === n.ref; })[0];
        return h('li', { class: 'na-row' },
          h('div', { class: 'na-body' },
            h('p', { class: 'na-note' }, h('span', { class: 'na-l', text: 'Your note' }), n.note),
            h('p', { class: 'na-ans' }, h('span', { class: 'na-l', text: 'Answer' }), n.answer)),
          h('div', { class: 'na-side' },
            h('span', { class: 'na-chip na-' + n.status, text: NOTE_CHIP[n.status] || NOTE_CHIP.done }),
            h('button', { class: 'link-btn na-go', 'aria-label': 'Go to ' + ((ref && ref.title) || n.ref), text: 'Go to ' + n.ref.toUpperCase(),
              onclick: function () { jumpTo('dz-' + n.ref); } })));
      })));
  }
  function renderRecapNote(b) {   // a recap with markable: false: a record to read, no controls (it used to draw nothing)
    return h('section', { class: 'blk blk-recap', id: 'dz-' + b.id }, h('h2', { text: b.title || 'Recap' }),
      h('ul', { class: 'recap' }, (b.items || []).map(function (x) { return h('li', { text: x }); })));
  }
  function renderSettled(b) {   // "Already decided": context only, never a control
    return h('section', { class: 'blk blk-settled' }, h('h2', { text: b.title || 'Already decided' }),
      h('div', { class: 'cards settled' }, b.items.map(function (it) {
        return h('article', { class: 'card settled-card' }, h('h3', { text: it.title }),
          it.quote ? h('blockquote', { class: 'sq', text: it.quote }) : null,
          it.rule ? h('p', { class: 'srule' }, h('span', { class: 'srule-l', text: 'Rule' }), it.rule) : null);
      })));
  }
  function renderGroup(r, a, g, i) {   // plan preset: a section per group, with "Approve the rest" when something in it can be approved
    var ed = isEditable();
    var btn = groupHasBulk(r.decisions, g.name) ? h('button', { class: 'btn grp-approve', disabled: ed ? null : true, onclick: function () {
      approveRestIds(r.decisions, g.name, S.draft.marks).forEach(function (id) { setMark(id, { status: 'approve' }); });
    } }, h('span', { text: 'Approve the rest' }), h('span', { class: 'grp-n' })) : null;
    var sec = h('section', { class: 'gsec', id: 'grp-' + i, 'data-group': g.name },
      h('div', { class: 'grp-head' }, h('h2', { text: g.name || 'Plan' }), h('span', { class: 'grp-count' }), btn));
    g.blocks.forEach(function (b) { append(sec, renderBlock(r, a, b)); });
    return sec;
  }

  // ---- the gate: the locked final approval ("Build") ----
  function renderGate(r, dec) {   // no card: Build lives in the footer with the reason in its tooltip; these are the approval's two lists
    var g = dec.gate, after = g.after || [], later = g.later || [];
    if (!after.length && !later.length) return null;
    return h('section', { class: 'blk gate', id: 'dz-gate' }, h('div', { class: 'gate-lists' },
      after.length ? h('div', { class: 'gate-list' }, h('h3', { text: 'What I need from you after approval' }), h('ul', null, after.map(function (x) { return h('li', { text: x }); }))) : null,
      later.length ? h('div', { class: 'gate-list' }, h('h3', { text: 'Later, not in this plan' }), h('ul', null, later.map(function (x) { return h('li', { class: 'later', text: x }); }))) : null));
  }
  var GATE_BTN = 'Build';   // short, so Send and Build both fit the footer on a phone
  function gateReason(gs, ed) {
    return !ed ? 'This round is locked.' : S.sending ? 'Sending…' : gs.ready ? 'Nothing is open or on Change.' : gs.reason;
  }
  function gateTitle(g, gs, ed) {   // the Build button's tooltip: why it is locked, or what pressing it does
    return gs.ready && ed && !S.sending ? GATE_BTN + ': ' + (g.text || 'send the approved plan to Hermes') : gateReason(gs, ed);
  }
  function updateGate() {
    var r = viewed(), g = r.gate || (DOC && DOC.dec.gate); if (!g) return;
    var btn = document.getElementById('gate-btn'); if (!btn) return;
    var gs = gateState(r.decisions, S.draft.marks, g), ed = isEditable();
    var ok = gs.ready && ed && !S.sending;
    btn.setAttribute('aria-disabled', ok ? 'false' : 'true');
    btn.title = gateTitle(g, gs, ed);
    btn.classList.toggle('is-disabled', !ok);
    var want = gateIcon(gs.ready, ed), cur = btn.querySelector('svg.ico');
    if (cur && cur.getAttribute('data-icon') !== want) { btn.replaceChild(icon(want), cur); }
    var lab = btn.querySelector('.gate-label'); if (lab) lab.textContent = S.sending ? 'Sending…' : GATE_BTN;
  }

  function renderTally(r) {   // lives in the footer
    var idx = r.decisions || [];
    if (!idx.length) return null;
    function count(id, color, long, short) {
      return h('span', { class: 't-count', title: long.trim() }, h('span', { class: 't-dot', style: { background: color } }), h('b', { id: id, text: '0' }),
        h('span', { class: 'tl', text: long }), h('span', { class: 'ts', text: short }));
    }
    return h('div', { class: 'tally', id: 'dz-tally', 'aria-label': 'Decisions so far' },
      count('c-approve', 'var(--ok)', ' approved', ''), count('c-change', 'var(--warn)', ' to change', ''), count('c-skip', 'var(--faint)', ' skipped', ''),
      h('span', { class: 't-count t-open' }, h('b', { id: 'c-open', text: String(idx.length) }),
        h('span', { class: 'tl', text: ' of ' + idx.length + ' open' }), h('span', { class: 'ts', text: '/' + idx.length + ' open' })));
  }
  // nav chips: a plan board jumps to its groups; a design board to each decision (D1, D2 …); green once resolved
  function renderDocNav(r, grouped) {
    if (grouped) {
      if (!grouped.groups.length) return null;
      return h('nav', { class: 'dnav t-nav', 'aria-label': 'Sections' }, grouped.groups.map(function (g, i) {
        return h('a', { href: '#grp-' + i, 'data-group': g.name, text: g.name || 'Plan',
          onclick: function (e) { e.preventDefault(); var t = document.getElementById('grp-' + i); if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
      }));
    }
    var idx = r.decisions || [];
    if (!idx.length) return null;
    return h('nav', { class: 'dnav t-nav', 'aria-label': 'Decisions' }, idx.map(function (d) {
      return h('a', { href: '#dz-' + d.id, 'data-id': d.id, text: d.nav || (d.id.toUpperCase() + (d.title ? ' · ' + d.title.slice(0, 22) : '')),
        onclick: function (e) { e.preventDefault(); var t = document.getElementById('dz-' + d.id); if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
    }));
  }
  function updateTally() {
    var fullTally = document.getElementById('dz-tally');
    var idx = viewed().decisions || [], marks = S.draft.marks || {}, c = { approve: 0, change: 0, skip: 0 };
    idx.forEach(function (d) {
      var s = entryKind(d) === 'question' ? (isResolved(d, marks[d.id]) ? 'approve' : null) : (marks[d.id] || {}).status;   // an answered question counts as approved
      if (c[s] !== undefined) c[s]++;
    });
    var open = idx.length - c.approve - c.change - c.skip;
    if (fullTally) {
      ['approve', 'change', 'skip'].forEach(function (k) { document.getElementById('c-' + k).textContent = c[k]; });
      document.getElementById('c-open').textContent = open;
    }
    var doneEl = document.getElementById('c-done'), totalEl = document.getElementById('c-total'), compact = document.getElementById('dz-tally-compact');
    if (doneEl) doneEl.textContent = c.approve + c.change + c.skip;
    if (totalEl) totalEl.textContent = idx.length;
    if (compact) compact.hidden = !idx.length;
    var done = {};
    idx.forEach(function (d) { done[d.id] = isResolved(d, marks[d.id]); });
    document.querySelectorAll('.t-nav a').forEach(function (a) {
      var ok = a.dataset.id ? !!done[a.dataset.id] : idx.filter(function (d) { return (d.group || '') === a.dataset.group && entryKind(d) !== 'defaults'; }).every(function (d) { return done[d.id]; });
      a.classList.toggle('done', ok);
    });
    var prog = document.getElementById('dz-progress');
    if (prog) prog.textContent = progressLine(idx, marks);
    document.querySelectorAll('.gsec').forEach(function (g) {
      var name = g.dataset.group, mine = idx.filter(function (d) { return (d.group || '') === name && entryKind(d) !== 'defaults'; });
      var n = approveRestIds(idx, name, marks).length;
      g.querySelector('.grp-count').textContent = mine.filter(function (d) { return done[d.id]; }).length + '/' + mine.length + ' resolved';
      var b = g.querySelector('.grp-approve'), gn = g.querySelector('.grp-n');
      if (gn) gn.textContent = n ? ' (' + n + ')' : '';
      if (b) b.disabled = !n || !isEditable();
    });
    updateGate();
    var end = document.getElementById('dz-end');
    if (end && DOC) end.textContent = endLine(idx, marks, DOC.dec.gate, DOC.dec.next, isEditable());
  }

  var STATUS_TEXT = { approve: 'Approved', change: 'Change', skip: 'Skipped' };
  function statusText(m) {
    return m.status ? STATUS_TEXT[m.status] + (m.option ? ' · ' + m.option.toUpperCase() : '') : m.option ? 'Picked ' + m.option.toUpperCase() : 'Open';
  }
  var TONES = ['neutral', 'ok', 'warn', 'err', 'accent'];
  var TONE_ICON = { neutral: 'info', accent: 'info', ok: 'check', warn: 'alert', err: 'stop' };
  function tone(t) { return TONES.indexOf(t) >= 0 ? t : 'neutral'; }
  function renderBlock(r, a, b) {
    var el;
    if (b.type === 'text') {
      el = h('section', { class: 'blk blk-text' }, b.title ? h('h2', { text: b.title }) : null, paras(b.text));
    } else if (b.type === 'cards') el = renderCards(b);
    else if (b.type === 'callout') el = renderCallout(b);
    else if (b.type === 'table') el = renderTable(b);
    else if (b.type === 'mock') el = renderContextMock(r, a, b);
    else if (b.type === 'artifact' && !b.markable) el = renderArtifact(r, b);
    else if (b.type === 'settled') el = renderSettled(b);
    else if (b.type === 'recap' && b.markable === false) el = renderRecapNote(b);
    else if (!isMarkableBlock(b)) el = null;
    else {
      var cls = 'blk item' + artifactDisplayClass(b);
      el = h('section', { class: cls, id: 'dz-' + b.id, 'data-id': b.id, 'data-kind': b.type });
      var head = h('div', { class: 'ihead' }, b.tag ? h('span', { class: 'tag', text: b.tag }) : b.type === 'question' ? h('span', { class: 'tag', text: 'Question' }) : null,
        h('h3', { text: b.title || (b.type === 'recap' ? 'Is this record right?' : b.type === 'defaults' ? 'How I read the rest' : b.id.toUpperCase()) }),
        b.revised ? h('span', { class: 'rev', text: b.revised }) : null, h('span', { class: 'status' }));
      append(el, head);
      append(el, renderAsked(b));
      if (b.type === 'recap') {
        append(el, h('ul', { class: 'recap' }, b.items.map(function (x) { return h('li', { text: x }); })));
        if (b.markable !== false) append(el, decideRow(b, false));   // missing flag = markable, same default as store.validate_decisions
      } else if (b.type === 'defaults') {
        if (b.text) append(el, paras(b.text, 'itext'));
        append(el, h('ul', { class: 'recap' }, b.items.map(function (x) { return h('li', { text: x }); })));
        append(el, decideRow(b, false));
      } else if (b.type === 'question') {
        append(el, renderQuestion(b));
      } else if (b.type === 'piece') {
        append(el, renderPiece(r, a, b));
      } else if (b.type === 'artifact') {
        append(el, [artifactBody(r, b), decideRow(b, false)]);
        leadWithCanvas(el, b);
      } else {
        if (b.today) append(el, h('p', { class: 'today' }, h('b', { text: 'Today: ' }), b.today));
        if (b.text) append(el, paras(b.text, 'itext'));
        append(el, renderOptions(r, a, b));
        append(el, decideRow(b, pickable(b)));
        append(el, h('ol', { class: 'ipins', id: 'pins-' + b.id }));
      }
      setTimeout(function () { refreshBlock(b.id); }, 0);
    }
    var sec = el && SECTIONS.get(b);
    if (sec) { el.setAttribute('data-mr-ref', sec.ref); el.setAttribute('data-mr-title', sec.title); }
    return el;
  }
  function renderAsked(b) {   // the "You asked / Answer" strip a later round carries
    if (!b.asked && !b.answer) return null;
    return h('div', { class: 'qa' },
      b.asked ? h('p', { class: 'qa-row' }, h('span', { class: 'qa-l', text: 'You asked' }), b.asked) : null,
      b.answer ? h('p', { class: 'qa-row' }, h('span', { class: 'qa-l', text: 'Answer' }), b.answer) : null);
  }
  function renderPiece(r, a, b) {   // title and tag sit in the head; text, items, an optional pinnable mock, then Approve / Change / Skip
    return [
      b.text ? paras(b.text, 'itext') : null,
      b.items && b.items.length ? h('ul', { class: 'recap piece-items' }, b.items.map(function (x) { return h('li', { text: x }); })) : null,
      b.html ? h('figure', { class: 'opt ctx-fig piece-fig' }, mockBox(r, a, b, { id: null, html: b.html })) : null,
      h('ol', { class: 'ipins', id: 'pins-' + b.id }),
      decideRow(b, false)];
  }
  // ---- a question: the recommendation, then "Go with recommendation" or "Different answer" (one line, 280 characters) ----
  function renderQuestion(b) {
    var ed = isEditable(), rec = b.recommendation || {}, id = b.id;
    function qbtn(c, label) {
      return h('button', { class: 'dbtn qbtn q-' + c, 'data-c': c, 'aria-pressed': 'false', disabled: ed ? null : true, text: label,
        onclick: function () { pickAnswer(id, c); } });
    }
    var input = h('input', { type: 'text', class: 'qinput', maxlength: String(LIMIT.custom), placeholder: 'Your answer, in one line', 'aria-label': 'Your answer', readonly: ed ? null : true });
    input.value = mark(id).custom || '';
    var count = h('span', { class: 'qcount', text: input.value.length + ' / ' + LIMIT.custom });
    input.addEventListener('input', function () {
      var v = capCustom(input.value);
      if (v !== input.value) input.value = v;
      count.textContent = v.length + ' / ' + LIMIT.custom;
      setCustom(id, v);
    });
    var custom = h('div', { class: 'qcustom' }, input, count);
    custom.hidden = mark(id).choice !== 'custom';
    var note = h('textarea', { class: 'dnote qnote', rows: '1', maxlength: '2000', placeholder: 'Note: anything to add', 'aria-label': 'Note', readonly: ed ? null : true });
    note.value = mark(id).note || '';
    note.addEventListener('input', function () { setMarkQuiet(id, note.value); autoGrow(note); });
    requestAnimationFrame(function () { autoGrow(note); });
    return [
      b.text ? paras(b.text, 'itext') : null,
      h('div', { class: 'qrec' }, h('span', { class: 'qrec-l', text: 'Recommendation' }), h('p', { class: 'qrec-a', text: rec.answer || '' }),
        rec.why ? h('p', { class: 'qrec-why', text: rec.why }) : null),
      h('div', { class: 'decide qdecide', 'data-id': id },
        h('div', { class: 'grp', role: 'group', 'aria-label': 'Answer' }, qbtn('recommended', 'Go with recommendation'), qbtn('custom', 'Different answer'))),
      custom, note];
  }
  function pickAnswer(id, c) {
    var m = mark(id);
    if (c === 'recommended') { 
      setMark(id, m.choice === 'recommended' ? { choice: null, custom: '' } : { choice: 'recommended', custom: '' }); 
      return; 
    }
    if (c === 'custom') {
      setMark(id, m.choice === 'custom' ? { choice: null, custom: '' } : { choice: 'custom', custom: m.custom || '' });
    }
    var el = document.getElementById('dz-' + id), inp = el && el.querySelector('.qinput');
    if (inp) setTimeout(function () { inp.focus({ preventScroll: true }); }, 0);
  }
  function setCustom(id, text) {   // typing: store and refresh the readouts, never rebuild the input
    var m = Object.assign({}, mark(id), { choice: 'custom', custom: text });
    S.draft.marks[id] = m;
    saveDraft(); updateSendbar(); updateTally();
    var el = document.getElementById('dz-' + id); if (el) refreshQuestion(el, id);
  }
  function refreshQuestion(el, id) {
    var m = mark(id);
    var done = m.choice === 'recommended' || (m.choice === 'custom' && !!String(m.custom || '').trim());
    el.querySelectorAll('.qbtn').forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.c === m.choice)); });
    var cu = el.querySelector('.qcustom'); if (cu) cu.hidden = m.choice !== 'custom';
    var st = el.querySelector('.status');
    st.dataset.s = done ? 'approve' : '';
    st.textContent = done ? 'Answered' : 'Open';
    el.classList.toggle('marked', done);
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
    function cellText(cell, value) {
      var text = String(value), re = /https?:\/\/[^\s<>"'\u0000-\u001f\u007f-\u009f]+/gi, match, end = 0;
      while ((match = re.exec(text))) {
        // Do not extract a URL from another scheme or a protocol-relative address.
        if (match.index && /[\w\/:@%+.-]/.test(text[match.index - 1])) continue;
        var label = match[0], pairs = { ')': '(', ']': '[', '}': '{' };
        // Leave sentence punctuation and unmatched closing brackets outside the link.
        while (label) {
          var last = label.slice(-1), open = pairs[last];
          if (/[.,!?;:]/.test(last) || (open && label.split(last).length > label.split(open).length)) label = label.slice(0, -1);
          else break;
        }
        var url = safeLink(label);
        if (!url) continue;
        cell.appendChild(document.createTextNode(text.slice(end, match.index)));
        var a = h('a', { text: label });
        secureLink(a, url);
        cell.appendChild(a);
        end = match.index + label.length;
      }
      cell.appendChild(document.createTextNode(text.slice(end)));
    }
    return h('section', { class: 'blk blk-table' }, b.title ? h('h2', { text: b.title }) : null,
      h('div', { class: 'tbl-wrap', tabindex: '0', role: 'region', 'aria-label': b.title || 'Table' },
        h('table', { class: 'tbl' },
          h('thead', null, h('tr', null, b.columns.map(function (c, j) { return h('th', { scope: 'col', class: cls(j), text: c }); }))),
          h('tbody', null, b.rows.map(function (row) {
            return h('tr', null, row.map(function (c, j) {
              var cell = h(j ? 'td' : 'th', { scope: j ? null : 'row', class: cls(j) });
              cellText(cell, c);
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

  // ---- an artifact: the agent's own page (HTML, CSS, script) in a sandboxed frame ----
  // sandbox="allow-scripts" with the same-origin permission withheld gives the page an opaque origin: it cannot reach this
  // board's DOM, storage, cookies or api/submit, and the CSP (store.ARTIFACT_CSP, as a meta in its own file and as a header
  // on every file of the round) leaves it no network. It loads from a served file (rounds/<n>/a/blocks/<id>.html), never
  // srcdoc: srcdoc would inherit this page's CSP and block its inline script. The page may keep bounded page state and
  // ask for a mark or a clipboard write through its frozen hook; the board accepts only its own block, on the round it is
  // showing, within the caps. A tap can open a pin draft, never send anything. Text and
  // captions are text (textContent), not markup. A page that navigates its own frame away from the block file is reset
  // to it (only the block file answers the probe's ping), a few times, then the frame is dropped.
  var ART = { min: 80, max: 4000, longMax: 30000, perSecond: 40, growths: 40, helloWait: 1000, resets: 3, file: /^blocks\/[a-z0-9-]+\.html$/ };
  var ART_CANVAS = { minZoom: .15, maxZoom: 2.5, fitMax: 1.1, margin: 24, step: 1.25, delta: 1200, perSecond: 144, slack: 240 };
  var artCanvasViews = new Map();   // preserve the view if the board rerenders for a device or a breakpoint
  function artifactDisplayClass(b) {
    return b && b.type === 'artifact' && ['page', 'long', 'canvas'].indexOf(b.display) >= 0 ? ' blk-art-' + b.display : '';
  }
  function leadWithCanvas(el, b) {
    if (b.display !== 'canvas') return;
    var fig = el.querySelector('.art-fig');
    if (fig && fig.querySelector('.art-canvas')) el.prepend(fig);
  }
  function paintArtifactCanvas(f) {
    var v = f.view;
    f.world.style.transform = 'translate(' + v.x + 'px, ' + v.y + 'px) scale(' + v.zoom + ')';
    f.percent.textContent = Math.round(v.zoom * 100) + '%';
    f.el.style.setProperty('--cv-x', v.x + 'px');   // the dotted ground moves and scales with the camera
    f.el.style.setProperty('--cv-y', v.y + 'px');
    f.el.style.setProperty('--cv-z', String(v.zoom));
    f.el.style.setProperty('--cv-a', v.zoom < .3 ? '0%' : '24%');
  }
  function setArtifactView(f, x, y, zoom) {
    // Clamp the camera, not only each frame's deltas, so a frame cannot park its own page out of reach.
    var w = f.el.clientWidth, h = f.el.clientHeight, sw = f.W * zoom, sh = (f.pageH || f.iframe.offsetHeight) * zoom;
    var keepX = Math.min(ART_CANVAS.slack, sw), keepY = Math.min(ART_CANVAS.slack, sh);
    x = num(x, keepX - sw, w - keepX);
    y = num(y, keepY - sh, h - keepY);
    var changedZoom = f.view.zoom !== zoom;
    f.view = { x: x, y: y, zoom: zoom };
    artCanvasViews.set(f.viewKey, f.view);
    paintArtifactCanvas(f);
    if (changedZoom) {
      clearTimeout(f.zoomTimer);
      f.zoomTimer = setTimeout(function () { if (f.el.isConnected && f.alive) pushArtifact(f); }, 120);
    }
  }
  function fitArtifactCanvas(f) {
    var w = f.el.clientWidth, hgt = f.el.clientHeight, pad = 2 * ART_CANVAS.margin;
    var height = f.pageH || f.iframe.offsetHeight;
    if (!w || !hgt || !f.W || !height) return;   // nothing measured yet: a fit now would divide by zero and clamp to max zoom
    var z = num(Math.min(ART_CANVAS.fitMax, (w - pad) / f.W, (hgt - pad) / height), ART_CANVAS.minZoom, ART_CANVAS.maxZoom);
    setArtifactView(f, (w - f.W * z) / 2, (hgt - height * z) / 2, z);
  }
  function zoomArtifactCanvas(f, zoom, x, y) {
    var v = f.view, z = num(zoom, ART_CANVAS.minZoom, ART_CANVAS.maxZoom);
    if (v.zoom === z) return;
    if (x === undefined) { x = f.el.clientWidth / 2; y = f.el.clientHeight / 2; }
    var ratio = z / v.zoom;
    setArtifactView(f, x - (x - v.x) * ratio, y - (y - v.y) * ratio, z);
  }
  function canvasSpace(f, down) {
    f.space = down;
    f.shield.hidden = !(f.hand || f.space || f.drag);
  }
  function artifactCanvasKey(f, key, down) {
    if (key === ' ') { canvasSpace(f, down); return true; }
    if (key !== '+' && key !== '-' && key !== '0' && key !== '1') return false;
    if (!down) return true;
    if (key === '0') fitArtifactCanvas(f);
    else zoomArtifactCanvas(f, key === '1' ? 1 : f.view.zoom * (key === '+' ? ART_CANVAS.step : 1 / ART_CANVAS.step));
    return true;
  }
  function artifactCanvasWheel(f, dx, dy, zoom, x, y) {
    dx = num(dx, -ART_CANVAS.delta, ART_CANVAS.delta);
    dy = num(dy, -ART_CANVAS.delta, ART_CANVAS.delta);
    if (zoom) zoomArtifactCanvas(f, f.view.zoom * Math.exp(-dy * .0105), x, y);
    else if (dx || dy) setArtifactView(f, f.view.x - dx, f.view.y - dy, f.view.zoom);
  }
  function canvasWheelPixels(mode, page) { return mode === 1 ? 16 : mode === 2 ? page : 1; }
  function artifactCanvas(f) {
    f.W = f.b.desktop_width || 1440;
    f.pinPositions = new Map();
    f.viewKey = f.r.n + ':' + f.b.id;
    f.view = artCanvasViews.get(f.viewKey) || { x: 0, y: 0, zoom: 1 };
    f.iframe.style.width = f.W + 'px';
    f.world = h('div', { class: 'canvas-world' }, f.iframe);
    f.shield = h('div', { class: 'canvas-shield', 'aria-hidden': 'true' });
    f.percent = h('output', { class: 'canvas-percent', 'aria-label': 'Zoom level', 'aria-live': 'off' });
    function control(label, image, run) {
      return h('button', { type: 'button', class: image ? 'icon-btn' : 'canvas-text-btn', title: label,
        'aria-label': label, onclick: run }, image ? icon(image) : label);
    }
    function setHand(on) {
      f.hand = !!on;
      hand.setAttribute('aria-pressed', String(f.hand));
      canvasSpace(f, f.space);
    }
    var hand = control('Hand tool', 'grab', function () {
      if (!f.hand && S.commentMode) setCommentMode(false);   // moving the canvas and pinning on it are one choice
      setHand(!f.hand);
    });
    f.setHand = setHand;
    hand.setAttribute('aria-pressed', 'false');
    var controls = h('div', { class: 'canvas-controls', role: 'group', 'aria-label': 'Canvas controls' },
      hand, control('Zoom out', 'minus', function () { artifactCanvasKey(f, '-', true); }), f.percent,
      control('Zoom in', 'plus', function () { artifactCanvasKey(f, '+', true); }),
      control('Fit', 'scan', function () { fitArtifactCanvas(f); }),
      control('100%', null, function () { zoomArtifactCanvas(f, 1); }));
    var el = h('div', { class: 'art art-canvas', role: 'region', 'aria-label': (f.b.title || 'Artifact') + ' canvas',
      tabindex: '0', 'data-mr-no-pin': '' }, f.world, f.shield, controls);
    f.el = el;   // the first paint sets the ground's camera variables on it
    canvasSpace(f, false);
    paintArtifactCanvas(f);
    el.addEventListener('wheel', function (e) {
      e.preventDefault();
      var r = el.getBoundingClientRect(), unit = canvasWheelPixels(e.deltaMode, el.clientHeight);
      artifactCanvasWheel(f, e.deltaX * unit, e.deltaY * unit, e.ctrlKey || e.metaKey, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    el.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey || (e.key === ' ' && e.target !== el)) return;
      if (artifactCanvasKey(f, e.key, true)) e.preventDefault();
    });
    el.addEventListener('keyup', function (e) { if (e.key === ' ') canvasSpace(f, false); });
    el.addEventListener('focusout', function () { canvasSpace(f, false); });
    el.addEventListener('pointerdown', function (e) {
      if (controls.contains(e.target) || (e.button !== 1 && !(e.button === 0 && (f.space || f.hand)))) return;
      e.preventDefault();
      f.drag = { id: e.pointerId, x: e.clientX - f.view.x, y: e.clientY - f.view.y };
      canvasSpace(f, f.space);
      el.setPointerCapture(e.pointerId);
      el.classList.add('canvas-dragging');
    });
    el.addEventListener('pointermove', function (e) {
      if (f.drag && f.drag.id === e.pointerId) setArtifactView(f, e.clientX - f.drag.x, e.clientY - f.drag.y, f.view.zoom);
    });
    el.addEventListener('lostpointercapture', function (e) {
      if (!f.drag || f.drag.id !== e.pointerId) return;
      f.drag = null;
      el.classList.remove('canvas-dragging');
      canvasSpace(f, f.space);
    });
    el.addEventListener('click', function (e) { if (e.target === f.shield) e.stopPropagation(); });
    el.addEventListener('auxclick', function (e) { if (e.button === 1) e.preventDefault(); });
    return el;
  }
  function artifactCanvasInput(f, d) {
    if (!f.canvas || !f.alive) return;
    // Keep keyboard releases independent of the high-frequency wheel bucket.
    var key = d.type === 'canvas-key', now = performance.now(), slot = key ? 'keysBudget' : 'wheelBudget';
    var budget = f[slot];
    if (!budget || now - budget.since >= 1000) budget = f[slot] = { since: now, count: 0 };
    if (key && d.key === ' ' && d.down === false) {
      if (f.pressed) { f.pressed = false; canvasSpace(f, false); }
      return;
    }
    var limit = key ? ART_CANVAS.perSecond / 2 : ART_CANVAS.perSecond;
    if (budget.count >= limit) return;
    budget.count++;
    if (key) {
      if (d.key === ' ' && d.down === true) { if (f.pressed) return; f.pressed = true; }
      if (typeof d.down === 'boolean') artifactCanvasKey(f, d.key, d.down);
      return;
    }
    if (![d.deltaX, d.deltaY, d.clientX, d.clientY].every(Number.isFinite) ||
        typeof d.ctrlKey !== 'boolean' || typeof d.metaKey !== 'boolean') return;
    var v = f.view;
    var drag = d.drag === true;
    artifactCanvasWheel(f, d.deltaX * (drag ? v.zoom : 1), d.deltaY * (drag ? v.zoom : 1), !drag && (d.ctrlKey || d.metaKey),
      v.x + num(d.clientX, 0, f.W) * v.zoom, v.y + num(d.clientY, 0, f.pageH) * v.zoom);
  }
  function artifactBody(r, b) {
    var f = null;
    var page = b.display === 'page', long = b.display === 'long', canvas = b.display === 'canvas';
    var fig = h('figure', { class: 'opt ctx-fig art-fig' });
    if (typeof b.file === 'string' && ART.file.test(b.file)) {
      f = { r: r, vid: b.id, b: b, device: canvas ? 'desktop' : S.width, ctx: 'artifact', alive: false, ghost: null, scale: 1, pageH: 0, count: 0, since: 0, grown: 0, resets: 0, watch: 0, page: page, long: long, canvas: canvas };
      var fixed = typeof b.height === 'number';
      f.iframe = h('iframe', { class: 'art-frame' + (page ? ' art-page-frame' : '') + (long ? ' art-long-frame' : ''), src: frameUrl(r, b.file),
        title: b.title || 'Artifact ' + b.id, sandbox: 'allow-scripts', allow: 'clipboard-write', referrerpolicy: 'no-referrer', loading: 'lazy' });
      // A page scrolls inside a viewport-height iframe. Long and canvas both follow the page's own height.
      f.iframe.style.height = page ? 'calc(100dvh - var(--barh, 52px) - var(--footh, 0px))'
        : (fixed && !long && !canvas ? num(b.height, ART.min, ART.max) : 120) + 'px';
      f.iframe.addEventListener('load', function () { artifactLoaded(f); });
      f.el = canvas ? artifactCanvas(f) : page || long ? h('div', { class: 'art ' + (page ? 'art-page' : 'art-long') }, f.iframe)
        : h('div', { class: 'art', style: b.desktop_width ? { maxWidth: b.desktop_width + 'px' } : null }, f.iframe);
      S.frames.push(f);
      fig.appendChild(f.el);
    } else {
      fig.appendChild(h('p', { class: 'art-missing', text: 'This artifact has no page to show.' }));
    }
    if (b.caption) fig.appendChild(h('figcaption', { class: 'ctx-cap', text: b.caption }));
    return [
      b.text && b.header !== false ? paras(b.text, 'itext') : null,
      fig,
      canComment() && S.commentMode && f && b.header !== false
        ? h('p', { class: 'art-hint', text: 'Comment mode: tap the artifact to comment on that spot. Press Exit comment in the header to use it.' }) : null,
      h('ol', { class: 'ipins', id: 'pins-' + b.id })];
  }
  function renderArtifact(r, b) {   // not a decision: a neutral block, no "mock" framing
    var bare = b.header === false;   // the page speaks for itself: no title, no text above it, no hint
    var el = h('section', { class: 'blk blk-art' + artifactDisplayClass(b) + (bare ? ' blk-art-bare' : ''), id: 'dz-' + b.id, 'data-id': b.id },
      b.title && !bare ? h('h2', { text: b.title }) : null, artifactBody(r, b));
    leadWithCanvas(el, b);
    setTimeout(function () { redrawPins(b.id); }, 0);
    return el;
  }
  function artifactLoaded(f) {   // every load of the frame, the block file's or whatever the page navigated itself to
    f.alive = false;
    if (f.canvas) { f.loaded = true; f.pinPositions.clear(); canvasSpace(f, false); }
    try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'ping' }, '*'); } catch (e) {}
    pushArtifact(f);
    clearTimeout(f.watch);
    f.watch = setTimeout(function () {
      if (f.alive || f.gone) return;   // the probe answered: this is the block file
      if (f.resets >= ART.resets) {    // it keeps leaving: show no page at all rather than a document we do not control
        f.gone = true;
        S.frames = S.frames.filter(function (x) { return x !== f; });
        f.el.textContent = '';
        f.el.appendChild(h('p', { class: 'art-missing', text: 'This artifact page did not stay on its own file, so it is not shown.' }));
        return;
      }
      f.resets++;
      f.iframe.src = frameUrl(f.r, f.b.file);
    }, ART.helloWait);
  }
  function pushArtifact(f) {
    var out = [];
    S.draft.pins.forEach(function (p, i) {
      if (p.block !== f.b.id || (f.canvas ? p.on === 'board' : p.w !== f.device) || !pinVisible(p)) return;
      out.push({i: i, n: pinNo(p, i), x: p.x, y: p.y, hermes: T.activityState(T.activityFor(p.thread, p)),
        queued: T.queuedBehind(T.activityFor(p.thread, p)), ...(f.canvas && p.target ? {target: cleanTarget(p.target)} : {})});
    });
    if (f.ghost) out.push({ i: -1, n: nextPinNo(), x: f.ghost.x, y: f.ghost.y, ghost: true });
    frameThreadEntries(f, f.b.id).forEach(function (t, j) {
      var a = t.anchor || {};
      out.push({ i: 60 + j, n: t.n, x: a.x || 0, y: a.y || 0, thread: t.id, detached: !!t.detached,
        hermes: T.activityState(T.activityFor(t.id)), queued: T.queuedBehind(T.activityFor(t.id)),
        ...(a.target ? {target: cleanTarget(a.target)} : {}) });
    });
    try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'state', comment: canComment() && S.commentMode,
      canvas: f.canvas, scale: f.canvas ? f.view.zoom : 1, pins: pinsOn() ? out : [] }, '*'); } catch (e) {}
  }
  function utf8(s) { try { return new TextEncoder().encode(s).length; } catch (e) { return s.length; } }
  function jsonBytes(v) { try { var s = JSON.stringify(v); return typeof s === 'string' ? utf8(s) : -1; } catch (e) { return -1; } }
  function pushPageState(f) {
    var ps = S.draft.page_state || {};
    try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'data', state: ps[f.b.id] || {} }, '*'); } catch (e) {}
  }
  function pageAck(f, cmd, id, ok, error, extra) {
    var m = { __mockreview: 1, cmd: cmd, id: id, ok: !!ok };
    if (error) m.error = String(error);
    if (extra) Object.keys(extra).forEach(function (k) { m[k] = extra[k]; });
    try { f.iframe.contentWindow.postMessage(m, '*'); } catch (e) {}
  }
  function pageData(f, d) {
    // Only the frame's own block, on the round the board is showing. A message from an old round is never replayed.
    if (f.r.n !== S.view || !f.b || f.b.type !== 'artifact') return;
    if (!Number.isInteger(d.id)) return;
    if (d.round !== undefined && d.round !== S.view) return;
    if (d.op === 'list') { pushPageState(f); return pageAck(f, 'data-ack', d.id, true); }
    if (d.op !== 'set' && d.op !== 'remove') return pageAck(f, 'data-ack', d.id, false, 'unknown data op');
    // Reads still work on a round that is closed or already sent; a write is refused instead of being dropped silently.
    if (!isEditable()) return pageAck(f, 'data-ack', d.id, false, 'this round is not editable');
    if (typeof d.key !== 'string') return pageAck(f, 'data-ack', d.id, false, 'key must be a string');
    var ps = S.draft.page_state || (S.draft.page_state = {}), b = f.b;
    var cur = Object.prototype.hasOwnProperty.call(ps, b.id) ? ps[b.id] : {};
    if (d.op === 'remove') {
      delete cur[d.key];
      if (Object.keys(cur).length) ps[b.id] = cur; else delete ps[b.id];
      saveDraft(); updateSendbar(); pushPageState(f); return pageAck(f, 'data-ack', d.id, true);
    }
    var s;
    try { s = JSON.stringify(d.value); } catch (e) { s = null; }
    if (typeof s !== 'string') return pageAck(f, 'data-ack', d.id, false, 'value is not JSON');
    if (utf8(s) > 8192) return pageAck(f, 'data-ack', d.id, false, 'value over 8 KB');
    if (!Object.prototype.hasOwnProperty.call(cur, d.key) && Object.keys(cur).length >= 64) {
      return pageAck(f, 'data-ack', d.id, false, 'block has 64 keys');
    }
    var next;
    try { next = JSON.parse(JSON.stringify(cur)); } catch (e) { next = {}; }
    put(next, d.key, JSON.parse(s));
    var total = 0;
    Object.keys(ps).forEach(function (id) { total += jsonBytes(id === b.id ? next : ps[id]); });
    if (!Object.prototype.hasOwnProperty.call(ps, b.id)) total += jsonBytes(next);
    if (total > 262144) return pageAck(f, 'data-ack', d.id, false, 'round state over 256 KB');
    put(ps, b.id, next);
    saveDraft(); updateSendbar(); pushPageState(f); pageAck(f, 'data-ack', d.id, true);
  }
  function pageMark(f, d) {
    if (f.r.n !== S.view || !f.b || f.b.type !== 'artifact') return;
    if (!Number.isInteger(d.id)) return;
    if (d.round !== undefined && d.round !== S.view) return;
    if (!isEditable()) return pageAck(f, 'mark-ack', d.id, false, 'this round is not editable');
    if (!isMarkableBlock(f.b)) return pageAck(f, 'mark-ack', d.id, false, 'this block is not markable');
    if (d.status !== 'approve' && d.status !== 'change' && d.status !== 'skip') {
      return pageAck(f, 'mark-ack', d.id, false, 'status must be approve, change or skip');
    }
    var current = mark(f.b.id);
    if (current.status && !pageOwned(f.b.id)) {   // the reviewer's gesture wins: a page may not flip it
      return pageAck(f, 'mark-ack', d.id, false, 'the reviewer already set a mark for this block');
    }
    var patch = { status: d.status }, noteKept = false;
    if (typeof d.note === 'string') {
      if (!(current.note || '').trim() || pageNoteOwned(f.b.id)) patch.note = d.note.slice(0, 2000);
      else noteKept = true;   // a reviewer-set note is never replaced
    }
    setMark(f.b.id, patch, true);
    pageAck(f, 'mark-ack', d.id, true, null, noteKept ? { note_kept: true } : null);
  }
  function artifactMessage(f, d) {
    if (d.type === 'canvas-wheel' || d.type === 'canvas-key') return artifactCanvasInput(f, d);
    var now = Date.now();
    if (now - f.since >= 1000) { f.since = now; f.count = 0; }
    if (++f.count > ART.perSecond) return;   // a frame that floods us is ignored, not obeyed
    var b = f.b;
    if (d.type === 'hello') {
      f.alive = true;
      if (f.long) f.pageTrue = Math.round(num(d.h, ART.min, 1000000));
      if (f.canvas) {
        var height = Math.round(num(d.h, 1, ART.longMax));
        if (height !== f.pageH) { f.pageH = height; f.iframe.style.height = height + 'px'; }
      } else if (!f.page && (f.long || typeof b.height !== 'number')) {
        var hh = Math.round(num(d.h, ART.min, f.long ? ART.longMax : ART.max));
        if (Math.abs(hh - f.pageH) >= 2 && f.grown < ART.growths) { f.grown++; f.pageH = hh; f.iframe.style.height = hh + 'px'; }
      }
      if (f.canvas && f.loaded && !artCanvasViews.has(f.viewKey)) fitArtifactCanvas(f);
      pushArtifact(f); pushPageState(f);
    } else if (d.type === 'data') {
      pageData(f, d);
    } else if (d.type === 'mark') {
      pageMark(f, d);
    } else if (d.type === 'positions' && f.canvas) {
      var entries = frameThreadEntries(f, b.id);
      // Layout reports can locate a marker on this canvas, but must not change stored anchor fractions.
      f.pinPositions.clear();
      (Array.isArray(d.pins) ? d.pins.slice(0, 60) : []).forEach(function (q) {
        if (!q || !Number.isInteger(q.i) || !Number.isFinite(q.x) || !Number.isFinite(q.y)) return;
        var t = entries[q.i - 60], p = S.draft.pins[q.i];
        if (t || (p && p.block === b.id && p.on !== 'board' && pinVisible(p))) {
          f.pinPositions.set(t ? t.id : p, {x: num(q.x, 0, 1), y: num(q.y, 0, 1)});
        }
      });
    } else if (d.type === 'click' || d.type === 'pin') {
      if (f.canvas && (!Number.isFinite(d.cx) || !Number.isFinite(d.cy) ||
          (d.type === 'pin' ? !Number.isInteger(d.i) : !Number.isFinite(d.x) || !Number.isFinite(d.y)))) return;
      var rc = f.iframe.getBoundingClientRect(), zoom = f.canvas ? f.view.zoom : 1;
      var anchor = { x: rc.left + num(d.cx, 0, f.canvas ? f.W : 4000) * zoom,
        y: rc.top + num(d.cy, 0, f.canvas ? f.pageH : f.long ? ART.longMax : 4000) * zoom };
      var box = {right: anchor.x, top: anchor.y};
      if (typeof d.thread === 'string' && d.thread) {
        if (f.canvas) openFrameThread(f, b.id, d.thread, d.detached, box);
        else openFrameThread(f, b.id, d.thread, d.detached);
        return;
      }
      if (d.type === 'pin') { editArtifactPin(f, num(d.i, -1, 99) | 0, anchor); return; }
      if (!canComment() || !S.commentMode) return;
      if (S.draft.pins.length >= LIMIT.pins) { toast('That’s ' + LIMIT.pins + ' pins — use the notes for the rest.', 'warn'); return; }
      var x = num(d.x, 0, 1), y = num(d.y, 0, 1);
      var target = cleanTarget(d.target);
      var mine = f.ghost = { x: x, y: y }; pushArtifact(f);
      notePop({ title: 'New pin · ' + pinWhat(b, null), where: target ? 'on ' + target.label : wlabel(f.device) + ' · ~' + pct(x) + ', ' + pct(y) + ' of the artifact', isNew: true, target: target,
        anchor: anchor, text: '',
        onClose: function () { if (f.ghost === mine) { f.ghost = null; pushArtifact(f); } },
        onSave: function (text, ta) {
          saveNewThread({ id: newPinId(), block: b.id, option: null, w: f.device, x: x, y: y, ...(target ? {target: target} : {}) }, text, ta);
          redrawPins(b.id);
        } });
    }
  }
  function editArtifactPin(f, i, anchor) {
    var b = f.b, box = {right: anchor.x, top: anchor.y};
    if (i >= 60) {
      var st = frameThreadEntries(f, b.id)[i - 60];
      if (st) openFrameThread(f, b.id, st.id, st.detached, f.canvas ? box : undefined);
      return;
    }
    var p = S.draft.pins[i];
    if (!p || p.block !== b.id || (f.canvas ? p.on === 'board' || !pinVisible(p) : p.w !== f.device)) return;
    if (p.thread && !p.thread.startsWith('tmp:')) { T.open(p.thread, {read: false, box: box}); return; }
    notePop({ pin: p, title: 'Pin ' + (i + 1) + ' · ' + pinWhat(b, null), where: wlabel(p.w) + ' · ~' + pct(p.x) + ', ' + pct(p.y),
      anchor: anchor, text: p.text,
      onSave: function (text) { p.text = text; if (p.thread) T.editThreadFirst(p.thread, text).catch(function () {}); saveDraft(); redrawPins(b.id); },
      onDelete: function () { if (p.thread) T.deleteThreadFirst(p.thread).catch(function () {}); S.draft.pins.splice(i, 1); saveDraft(); MOCKS.forEach(drawPins); redrawPins(b.id); } });
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
    var hostEl = h('div', { class: 'mock-host' + (isNew(viewed()) && (!S.commentMode || !pinsOn()) ? ' use' : '') });
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
    m.root = root;
    MOCKS.push(m);
    m.anchorSurface = surface(root, pins, { enabled: function () { return S.commentMode; },
      box: function () { return m.inner.getBoundingClientRect(); },
      pins: function () { return S.draft.pins.filter(function (p) { return p.block === m.b.id && p.option === m.o.id && p.w === S.width; }); },
      draw: function () { drawPins(m); }, changed: saveDraft });
    // A mock form must never navigate the board, CSP or not.
    sr.addEventListener('submit', function (e) { e.preventDefault(); }, true);
    // Comment mode: a press inside a native control must not focus it, open a select or drag a range (mockClick pins it
    // and cancels its click), and Enter or Space must not activate it. Only there: a press on ordinary content keeps
    // its text selection, and with it quote pins. Nothing stops propagating, so the board's own listeners still run.
    sr.addEventListener('mousedown', function (e) {
      if (canComment() && S.commentMode && !mockLive(e) && closestOf(e.composedPath()[0], CONTROL)) e.preventDefault();
    }, true);
    function mockKey(e) { if (canComment() && S.commentMode && !mockLive(e) && presses(e.key, e.composedPath()[0])) e.preventDefault(); }
    sr.addEventListener('keydown', mockKey, true);
    sr.addEventListener('keyup', mockKey, true);
    inner.addEventListener('click', function (e) { mockClick(m, e); });
    if (window.ResizeObserver) { m.ro = new ResizeObserver(function () { fit(m); }); m.ro.observe(outer); m.ro.observe(inner); }
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
  // A deleted thread always takes its marker away; a resolved one only with hide_resolved_pins on.
  // A pin that is a thread wears the number the server gave that thread; a pin that is not one keeps its place in the
  // list (the feedback text names those by place). Numbering every pin by place made a new pin and a carried thread both
  // read "1".
  // A pin still saving (`tmp:` id) is about to become the next thread, so it shows that number, not a wrong one.
  function pinNo(p, i) { return p && p.thread ? (T.number(p.thread) || nextPinNo()) : i + 1; }
  function nextPinNo() {
    var placed = S.draft.pins.filter(function (q) { return String(q.thread || '').indexOf('tmp:') !== 0; }).length;
    return Math.max(placed, T.threads().length) + 1;
  }
  function pinVisible(p) { return !p.thread || !(T.gone(p.thread) || (CFG.hide_resolved_pins && T.resolved(p.thread))); }
  function draftThreadIds() {
    var out = {};
    function add(list) { (list || []).forEach(function (p) { if (p.thread) out[p.thread] = true; }); }
    if (S.draft) { add(S.draft.pins); Object.keys(S.draft.variants || {}).forEach(function (k) { add(S.draft.variants[k].pins); }); }
    return out;
  }
  function drawPins(m) {
    m.pins.textContent = '';
    drawSavedPins(m);
    if (m.ghost) m.pins.appendChild(m.ghost);   // a new pin's ghost is not in the draft yet: keep it through a redraw
  }
  function drawSavedPins(m) {
    if (!pinsOn()) return;
    S.draft.pins.forEach(function (p, i) {
      if (p.block !== m.b.id || p.option !== m.o.id || p.w !== S.width || !pinVisible(p)) return;
      var box = m.inner.getBoundingClientRect();
      var spot = p.detached && p.target ? {left: ((p.target.doc.x - scrollX - box.left) / m.scale) + 'px', top: ((p.target.doc.y - scrollY - box.top) / m.scale) + 'px'} : {left: (p.x * 100) + '%', top: (p.y * 100) + '%'};
      var button = h('button', { class: 'mpin' + (p.detached ? ' det' : '') + (p.thread && T.unread(p.thread) ? ' un' : ''), style: spot, title: p.detached ? 'Element removed' : p.text, 'aria-label': 'Pin ' + pinNo(p, i) + ': ' + (p.detached ? 'Element removed · ' : '') + p.text, 'data-pin-id': p.id, ...(p.thread && String(p.thread).indexOf('tmp:') !== 0 ? {'data-thread': p.thread} : {}),
        onclick: function (e) { e.stopPropagation(); if (p.thread && String(p.thread).indexOf('tmp:') !== 0) { T.open(p.thread, {detached: p.detached}); } else editPin(m, i, e); } }, h('span', { text: String(pinNo(p, i)) }));
      m.pins.appendChild(T.paintPin(button, T.activityFor(p.thread, p)));
    });
    T.mockEntries(m.b.id, m.o.id).forEach(function (t) {
      if (!S.commentMode && !T.activityState(T.activityFor(t.id))) return;
      var box = m.inner.getBoundingClientRect(), s = T.spot(t, m.root, box);
      var style = s.doc
        ? {left: ((s.doc.x - scrollX - box.left) / m.scale) + 'px', top: ((s.doc.y - scrollY - box.top) / m.scale) + 'px'}
        : {left: (s.x * 100) + '%', top: (s.y * 100) + '%'};
      m.pins.appendChild(T.marker(t, style, s.detached || t.detached));
    });
  }
  function redrawPins(id) {
    MOCKS.forEach(function (m) { if (m.b.id === id) { drawPins(m); m.anchorSurface.schedule(); } });
    var canvas = S.frames.find(function (f) { return f.canvas && f.b.id === id; });
    S.frames.forEach(function (f) { if (f.ctx === 'artifact' && f.b.id === id) pushArtifact(f); });
    var list = document.getElementById('pins-' + id);
    if (list) {
      list.textContent = '';
      S.draft.pins.forEach(function (p, i) {
        if (p.block !== id || !pinVisible(p)) return;
        list.appendChild(h('li', null, h('button', { class: 'pin-row', onclick: function () {
          if (!p.thread) return focusDzPin(i);
          var box = canvas ? focusArtifactCanvas(p) : null;
          if (!box) focusLongPin(p);
          T.open(p.thread, {detached: p.detached, box: box || undefined});
        } },
          h('span', { class: 'pin-num', text: String(pinNo(p, i)) }),
          h('span', { class: 'pin-body' }, h('span', { class: 'pin-where', text: (p.option ? p.option.toUpperCase() + ' · ' : '') + wlabel(p.w) }), h('span', { class: 'pin-text', text: p.text })))));
      });
      if (S.commentMode) (canvas ? frameThreadEntries(canvas, id) : id === 'proto' ? T.protoEntries() : T.mockEntries(id, null)).forEach(function (t) {
        var row = T.row(t);
        if (canvas) row.firstElementChild.onclick = function () {
          T.open(t, {box: focusArtifactCanvas(t.anchor, t.id)});
        };
        list.appendChild(row);
      });
    }
    updateSendbar();
    if (S.draft.pins.some(function (p) { return p.on === 'board' && p.block === id; })) scheduleCanvasPins();
  }

  function startThread(pin, list, text, ta) {
    pin.thread = 'tmp:' + uuid();
    list.push(pin);
    saveDraft();
    redrawThreadSurfaces();
    T.create(pin, text, ta).then(function (thread) {
      pin.thread = thread.id;
      saveDraft();
      redrawThreadSurfaces();
    }, function (e) {
      var i = list.indexOf(pin);
      if (i >= 0) list.splice(i, 1);
      saveDraft();
      redrawThreadSurfaces();
      toast(e.message || 'Could not post the comment', 'warn');
    });
  }
  function saveNewThread(pin, text, ta) {
    pin.text = text;
    startThread(pin, S.draft.pins, text, ta);
  }
  function redrawThreadSurfaces() {
    if (!S.review || !S.draft) return;
    MOCKS.forEach(drawPins);
    S.frames.forEach(function (f) { if (f.ctx === 'artifact') pushArtifact(f); });
    if (isNew(viewed())) {
      if (viewed().kind === 'prototype') S.frames.forEach(function (f) { if (f.ctx === 'proto') pushProto(f); });
      else redrawCanvasPins();
    } else {
      Object.keys(S.draft.variants || {}).forEach(function (vid) {
        var list = document.getElementById('pins-' + vid);
        if (list) fillPinList(list, vid);
      });
      pushAll();
    }
    updateSendbar();
  }

  var pinRounds = {}, pinRefreshTimer = 0, pinRefreshing = false, pinDirty = false, pinReset = false;
  function pinRequests() { return pinRounds[S.view] || {}; }
  function applyPinSnapshot(st) {
    pinRounds = Object.fromEntries(st.rounds.map(function (r) { return [r.n, r.pin_requests || {}]; }));
  }
  // ---- a block Hermes patched: swap it in place, no reload ----
  function blockRevs(r) {
    var out = {};   // state.json's blocks_rev covers every block, not only the markable ones the index lists
    Object.keys(r.blocks_rev || {}).forEach(function (id) { out[id] = Number(r.blocks_rev[id]) || 0; });
    return out;
  }
  // The blocks whose revision in `st` is newer than the one the loaded round shows, as [{r, ids}] for the round on
  // screen. Any other round (or every round, when the page is about to re-render anyway) only forgets its cached
  // decisions.json, so its next render loads the patched one. A stale or repeated event finds nothing newer.
  function staleBlocks(st, rendering) {
    var out = [];
    st.rounds.forEach(function (r) {
      var known = assetRevs[r.n];
      if (!known) return;
      var now = blockRevs(r), ids = Object.keys(now).filter(function (id) { return now[id] > (known[id] || 0); });
      if (!ids.length) return;
      if (!rendering && DOC && DOC.el.isConnected && DOC.r.n === r.n && S.view === r.n) out.push({r: r, ids: ids});
      else { delete ASSETS[r.n]; delete assetRevs[r.n]; }
    });
    return out;
  }
  function swapBlocks(r, ids) {
    var seq = renderSeq;
    delete ASSETS[r.n]; delete assetRevs[r.n];
    loadAssets(r).then(function (a) {
      if (seq !== renderSeq || !DOC || !DOC.el.isConnected || DOC.r.n !== r.n) return;   // a full render took over
      var ok = true;
      keepPlace(function () { ok = ids.every(function (id) { return swapBlock(r, a, id); }); });
      if (!ok) keepPlace(render);   // the block could not be replaced where it stands: rebuild the page (assets are fresh)
      else paintPinActivity();
    }).catch(function () { keepPlace(render); });
  }
  function swapBlock(r, a, id) {
    var dec = a && a.dec, idx = dec && Array.isArray(dec.blocks) ? dec.blocks.findIndex(function (x) { return x.id === id; }) : -1;
    if (idx < 0) return false;
    var old = Array.from(DOC.el.querySelectorAll('[data-mr-ref]')).find(function (el) { return el.getAttribute('data-mr-ref') === id; });
    if (!old || !old.parentNode) return false;
    var secs = Array.isArray(r.sections) && r.sections.length === dec.blocks.length ? r.sections : derivedSections(dec);
    SECTIONS = new Map();
    dec.blocks.forEach(function (b, i) { if (secs[i]) SECTIONS.set(b, secs[i]); });
    var el = renderBlock(r, a, dec.blocks[idx]);   // marks and pins live in the draft, so they come straight back
    if (!el) return false;
    MOCKS = MOCKS.filter(function (m) {
      if (m.b.id !== id || !old.contains(m.outer)) return true;
      if (m.anchorSurface) m.anchorSurface.dispose();
      if (m.ro) m.ro.disconnect();
      return false;
    });
    S.frames = S.frames.filter(function (f) {
      if (f.ctx !== 'artifact' || f.b.id !== id) return true;
      clearTimeout(f.watch);
      return false;
    });
    CANVAS.forEach(function (c, sec) { if (sec === old || old.contains(sec)) CANVAS.delete(sec); });
    old.replaceWith(el);
    DOC.r = r; DOC.dec = dec;
    if (CANVAS_LIVE) CANVAS_LIVE.schedule();
    return true;
  }
  function refreshPinActivity(reset) {
    pinDirty = true;
    pinReset = pinReset || reset;
    if (pinRefreshing || pinRefreshTimer) return;
    // A burst can publish queued, delivered and answered before the browser has painted any of them.
    pinRefreshTimer = setTimeout(function () {
      var reload = pinReset;
      pinRefreshTimer = 0;
      pinReset = pinDirty = false;
      pinRefreshing = true;
      fetchState().then(function (st) {
        applyPinSnapshot(st);
        var moved = reload || st.current_round !== R().current_round || st.status !== R().status;
        var swaps = staleBlocks(st, moved);
        if (moved) {
          applyState(st);
          if (!st.rounds.some(function (r) { return r.n === S.view; })) switchRound(st.current_round);
          else keepPlace(render);
          if (reload) T.refresh();
        } else if (swaps.length) {
          applyState(st);
          swaps.forEach(function (sw) { swapBlocks(sw.r, sw.ids); });
        }
        T.pinActivityChanged();
      }).catch(function (e) {
        if (e.status === 403 || e.status === 404) { pinRounds = {}; T.pinActivityChanged(); }
      }).finally(function () {
        pinRefreshing = false;
        if (pinDirty) refreshPinActivity();
      });
    }, 40);
  }
  function blockLabel(id) {
    var sec = canvasSectionFor({block: id});
    return sec && sec.getAttribute('data-mr-title') || id;
  }
  function openPinFix(q) {
    var btn = Array.from(app.querySelectorAll('.mpin[data-pin-request], .mpin[data-pin-id]')).find(function (el) {
      return el.dataset.pinRequest === q.id || el.dataset.pinId === (q.pin_id || q.id);
    });
    if (btn) btn.scrollIntoView({block: 'nearest'});
    T.openFix(q, btn);
  }
  function paintHermesBlocks() {
    if (!DOC || !DOC.el.isConnected || DOC.r.n !== S.view) return;
    var byBlock = new Map();
    T.roundRequests().forEach(function (q) {
      if (!byBlock.has(q.block)) byBlock.set(q.block, []);
      byBlock.get(q.block).push(q);
    });
    DOC.el.querySelectorAll('[data-mr-ref]').forEach(function (sec) {
      var block = sec.getAttribute('data-mr-ref'), requests = byBlock.get(block) || [];
      var heading = sec.querySelector(':scope > .ihead');
      var chipHost = heading || sec;
      var chip = chipHost.querySelector(':scope > .hermes-block-status');
      var live = requests.filter(T.working);
      if (!live.length) {
        if (chip) chip.remove();
      } else {
        // When every working request here waits behind an older one elsewhere, say so with the shortest wait.
        var ahead = Math.min.apply(null, live.map(T.queuedBehind));
        var chipText = ahead ? T.queuedText(ahead) : 'Hermes is working…';
        if (!chip) {
          chip = h('span', {class: 'hermes-block-status', role: 'status', text: chipText});
          if (heading) heading.appendChild(chip); else sec.prepend(chip);
        } else if (chip.textContent !== chipText) chip.textContent = chipText;
      }
      var fixes = requests.filter(function (q) {
        return pinsOn() && !q.thread_id && (T.activityState(q) || q.status === 'done') && !S.draft.pins.some(function (p) {
          return p.id === (q.pin_id || q.id) && p.block === block && (p.option || null) === (q.option || null) &&
            p.w === S.width && pinVisible(p);
        });
      });
      var layer = sec.querySelector(':scope > .hermes-fix-layer');
      if (!fixes.length) { if (layer) layer.remove(); return; }
      if (!layer) layer = sec.appendChild(h('div', {class: 'pin-layer hermes-fix-layer'}));
      var previous = new Map(Array.from(layer.children, function (el) { return [el.dataset.pinRequest, el]; }));
      fixes.forEach(function (q, i) {
        var button = previous.get(q.id);
        if (!button) {
          button = layer.appendChild(h('button', {class: 'mpin', type: 'button', 'data-pin-request': q.id,
            'aria-label': 'Pin fix · ' + blockLabel(block), title: 'Pin fix',
            onclick: function (e) { e.stopPropagation(); T.openFix(q, button); }}, h('span')));
        }
        previous.delete(q.id);
        var spot = q.position || {x: Math.min(.85, .5 + i * .06), y: .5};
        button.style.left = (spot.x * 100) + '%';
        button.style.top = (spot.y * 100) + '%';
        button.firstElementChild.textContent = String(i + 1);
        T.paintPin(button, q);
      });
      previous.forEach(function (el) { el.remove(); });
    });
  }
  function paintPinActivity() {
    var from = document.activeElement;
    var focus = from && from.matches('.mpin') && (from.dataset.thread || from.dataset.pinId || from.dataset.pinRequest);
    redrawThreadSurfaces();
    paintHermesBlocks();
    if (pop && pop.paintActivity) pop.paintActivity();
    if (focus && !from.isConnected) {
      var marker = Array.from(app.querySelectorAll('.mpin')).find(function (el) {
        return (el.dataset.thread || el.dataset.pinId || el.dataset.pinRequest) === focus;
      });
      if (marker) marker.focus({preventScroll: true});
    }
  }

  // ---- pins on the canvas: every rendered section, not only mocks ----
  function sectionOf(el) { return closestOf(el, '[data-mr-ref]'); }
  function sectionRef(el) {
    var sec = sectionOf(el);
    return sec ? (Element.prototype.getAttribute.call(sec, 'data-mr-ref') || '') : '';
  }
  function canvasLayer(sec) {
    var found = CANVAS.get(sec);
    if (found) return found;
    var layer = h('div', { class: 'pin-layer' });
    var list = h('ol', { class: 'ipins canvas-pins' });
    sec.appendChild(layer);
    sec.appendChild(list);
    found = { layer: layer, list: list };
    CANVAS.set(sec, found);
    return found;
  }
  function canvasSections(ref) {
    if (!DOC || !DOC.el) return [];
    var all = [];
    if (Element.prototype.matches.call(DOC.el, '[data-mr-ref]')) all.push(DOC.el);
    all = all.concat(Array.prototype.slice.call(DOC.el.querySelectorAll('[data-mr-ref]')));
    return all.filter(function (el) {
      return Element.prototype.getAttribute.call(el, 'data-mr-ref') === ref;
    });
  }
  function canvasSectionFor(p) { return canvasSections(p.block)[0] || null; }   // refs name exactly one surface
  function canvasBox(sec) {   // the box a pin's x/y are a fraction of: client coords, padding-edge origin
    var sr = rectOf(sec);
    if (!sr) return null;
    var top = sr.top + (sec.clientTop || 0), ht = sec.clientHeight || 0;
    for (var i = 0; i < sec.children.length; i++) {
      var k = sec.children[i];
      if (!Element.prototype.matches.call(k, '.ipins')) continue;
      var kr = rectOf(k);
      if (kr && kr.height) { ht = kr.top - top; break; }   // first VISIBLE pin list: content ends here
    }
    return { left: sr.left + (sec.clientLeft || 0), top: top, width: sec.clientWidth || sr.width, height: ht };
  }
  function canvasLifecycle(doc) {   // one observer lifecycle per rendered doc, disposed on the next render
    var pending = 0, disposed = false;
    var observer = new MutationObserver(schedule);
    var resize = new ResizeObserver(schedule);
    var observed = new Set();
    var settleKey = '', settleCalm = 2, settleLeft = 0;
    function measureKey() {
      // The measured boxes and the pins they produced. A late layout (a font, a mock frame settling, keepPlace)
      // can move a section or an element without changing any box the observers watch; re-measuring this key
      // until it repeats is what notices a plain move.
      var out = [];
      CANVAS.forEach(function (c, sec) {
        var b = canvasBox(sec);
        out.push(b ? [b.left, b.top, b.width, b.height] : null);
      });
      if (S.draft && S.draft.pins) S.draft.pins.forEach(function (p) {
        out.push([num(p.x, 0, 1), num(p.y, 0, 1), !!p.detached,
          p.target && p.target.doc ? [p.target.doc.x, p.target.doc.y] : null]);
      });
      return JSON.stringify(out);
    }
    function settle() {
      pending = 0;
      if (disposed) return;
      observer.disconnect();   // the redraw writes to this same subtree: never retrigger it
      if (DOC && DOC.el === doc) stamp(doc);
      try { redrawCanvasPins(true); }
      finally {
        if (disposed) return;
        observer.observe(doc, { subtree: true, childList: true, characterData: true, attributes: true });
        observeAll();
        var key = measureKey();
        if (key !== settleKey) { settleKey = key; settleCalm = 0; settleLeft = 6; }
        else if (settleCalm < 2) settleCalm++;
        if (settleLeft > 0 && settleCalm < 2) { settleLeft--; schedule(); }
      }
    }
    function observeAll() {
      var watched = new Set([doc]);
      Array.prototype.forEach.call(doc.children, function (el) {
        if (el && Element.prototype.hasAttribute.call(el, 'data-mr-ref')) watched.add(el);
      });
      if (DOC && DOC.el === doc && S.draft && S.draft.pins) {
        S.draft.pins.forEach(function (p) {
          if (p.on !== 'board' || p.w !== S.width || !p.target) return;
          var sec = canvasSectionFor(p);
          var el = sec ? resolve(sec, p.target) : null;
          if (el) watched.add(el);
        });
      }
      observed.forEach(function (el) { if (!watched.has(el)) { resize.unobserve(el); observed.delete(el); } });
      watched.forEach(function (el) { if (!observed.has(el)) { resize.observe(el); observed.add(el); } });
    }
    function schedule() { if (!pending && !disposed) pending = requestAnimationFrame(settle); }
    addEventListener('scroll', schedule, true);
    addEventListener('resize', schedule);
    return { schedule: schedule, dispose: function () {
      disposed = true;
      cancelAnimationFrame(pending);
      observer.disconnect();
      resize.disconnect();
      removeEventListener('scroll', schedule, true);
      removeEventListener('resize', schedule);
    } };
  }
  function scheduleCanvasPins() {
    if (CANVAS_LIVE) CANVAS_LIVE.schedule(); else redrawCanvasPins();
  }
  // The yellow quote treatment anchors.js gives a mock, on the board document: the same registry, the same fallback.
  var CANVAS_HL = {native: !!(window.CSS && window.CSS.highlights && window.Highlight), ranges: [], marks: [], style: null};
  function canvasHlStyle() {
    if (CANVAS_HL.style && CANVAS_HL.style.isConnected) return;
    var s = h('style', {id: 'mr-canvas-highlight'});
    s.textContent = '::highlight(mr-comments){background:#fde68a;color:#18181b}mark[data-mr-highlight]{background:#fde68a;color:#18181b}';
    (document.head || document.documentElement).appendChild(s);
    CANVAS_HL.style = s;
  }
  function clearCanvasHighlights() {
    if (CANVAS_HL.native) {
      var reg = window.CSS.highlights.get('mr-comments');
      CANVAS_HL.ranges.forEach(function (r) { if (reg) reg.delete(r); });
    } else if (DOC && DOC.el) {
      CANVAS_HL.marks = Array.prototype.slice.call(DOC.el.querySelectorAll('mark[data-mr-highlight]'));
    }
    CANVAS_HL.marks.forEach(function (mark) {
      if (!mark.parentNode) return;
      var parent = mark.parentNode;
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
      parent.removeChild(mark);
      parent.normalize();
    });
    CANVAS_HL.marks = []; CANVAS_HL.ranges = [];
  }
  function showCanvasRange(range) {
    if (!range || !DOC || !DOC.el) return;
    canvasHlStyle();
    if (CANVAS_HL.native) {
      var reg = window.CSS.highlights.get('mr-comments');
      if (!reg) { reg = new window.Highlight(); window.CSS.highlights.set('mr-comments', reg); }
      reg.add(range); CANVAS_HL.ranges.push(range);
      return;
    }
    // The fallback wraps text slices only, exactly as anchors.js does: never a native control, never a stamped identity.
    var walk = document.createTreeWalker(DOC.el, NodeFilter.SHOW_TEXT), nodes = [];
    while (walk.nextNode()) {
      var node = walk.currentNode, parent = node.parentElement;
      if (range.intersectsNode(node) && node.textContent && !(parent && closestOf(parent, 'style,script'))) nodes.push(node);
    }
    nodes.forEach(function (node) {
      var r = document.createRange(); r.selectNodeContents(node);
      if (node === range.startContainer) r.setStart(node, range.startOffset);
      if (node === range.endContainer) r.setEnd(node, range.endOffset);
      if (r.collapsed) return;
      var mark = h('mark', {'data-mr-highlight': ''});
      try { r.surroundContents(mark); CANVAS_HL.marks.push(mark); } catch (e) {}
    });
  }
  function redrawCanvasPins(refreshPass) {
    var before = refreshPass && S.draft && S.draft.pins ? JSON.stringify(S.draft.pins) : null;
    clearCanvasHighlights();
    var boxes = new Map();
    CANVAS.forEach(function (c, sec) {
      boxes.set(sec, canvasBox(sec));   // with the lists laid out: drawing restores the same rows afterwards
    });
    if (!DOC || !S.draft || !pinsOn()) {
      CANVAS.forEach(function (c) { c.layer.textContent = ''; c.list.textContent = ''; });
      return;
    }
    var items = [];
    S.draft.pins.forEach(function (p, i) {
      if (p.on !== 'board' || !pinVisible(p)) return;
      var sec = canvasSectionFor(p);
      if (!sec) return;
      var detached = false, spot = null;
      var box = boxes.get(sec) || canvasBox(sec);
      if (p.w === S.width || p.target) {   // an element anchor follows its element on every width
        var scroll = scrollPoint();
        if (p.target) {
          stamp(sec);
          var el = resolve(sec, p.target), er = el ? rectOf(el) : null;
          var range = el && p.target.quote ? rangeFor(el, p.target) : null;
          var word = range ? range.getBoundingClientRect() : null;
          if (range) showCanvasRange(range);
          if (el && box && box.width && box.height && er && er.width && er.height && (!p.target.quote || word)) {
            var fr = p.target.quote ? word : er;
            var fx = p.target.quote ? 0.5 : num(p.target.x, 0, 1);
            var fy = p.target.quote ? 0.5 : num(p.target.y, 0, 1);
            p.x = num((fr.left - box.left + fx * fr.width) / box.width, 0, 1);
            p.y = num((fr.top - box.top + fy * fr.height) / box.height, 0, 1);
            p.target.doc = { x: num(box.left + p.x * box.width + scroll.x, 0, 1000000), y: num(box.top + p.y * box.height + scroll.y, 0, 1000000) };
          } else {
            detached = true;
            if (!docPoint(p.target.doc) && box && box.width) {   // legacy: derive the missing page spot once
              p.target.doc = { x: num(box.left + num(p.x, 0, 1) * box.width + scroll.x, 0, 1000000),
                               y: num(box.top + num(p.y, 0, 1) * (box.height || 0) + scroll.y, 0, 1000000) };
            }
          }
          spot = docPoint(p.target.doc);
        } else {   // no target: the page spot is the truth, the fraction is derived from it
          if (!docPoint(p.doc) && box && box.width) {
            p.doc = { x: num(box.left + num(p.x, 0, 1) * box.width + scroll.x, 0, 1000000),
                      y: num(box.top + num(p.y, 0, 1) * (box.height || 0) + scroll.y, 0, 1000000) };
          }
          spot = docPoint(p.doc);
          if (spot && box && box.width && box.height) {
            p.x = num((spot.x - scroll.x - box.left) / box.width, 0, 1);
            p.y = num((spot.y - scroll.y - box.top) / box.height, 0, 1);
          }
        }
      }
      items.push({ i: i, p: p, sec: sec, detached: detached, box: box, spot: spot });
    });
    CANVAS.forEach(function (c) {
      c.layer.textContent = '';
      c.list.textContent = '';
    });
    items.forEach(function (it) {
      var p = it.p;
      if (p.w !== S.width && !p.target) return;
      if (it.detached) p.detached = true; else delete p.detached;
      var c = canvasLayer(it.sec);
      if (it.box) { c.layer.style.height = it.box.height + 'px'; c.layer.style.bottom = 'auto'; }
      var style;
      if ((it.detached || !p.target) && it.spot && it.box) {   // a page point, not a fraction of the current box
        var scroll = scrollPoint();
        style = { left: num(it.spot.x - scroll.x - it.box.left, -1000000, 1000000) + 'px',
                  top: num(it.spot.y - scroll.y - it.box.top, -1000000, 1000000) + 'px' };
      } else {
        style = { left: (p.x * 100) + '%', top: (p.y * 100) + '%' };
      }
      var button = h('button', {
        class: 'mpin' + (it.detached ? ' det' : '') + (p.thread && T.unread(p.thread) ? ' un' : ''), style: style,
        title: it.detached ? 'Element removed' : p.text,
        'aria-label': 'Pin ' + pinNo(p, it.i) + ': ' + (it.detached ? 'Element removed · ' : '') + p.text,
        'data-mr-pin': String(it.i),
        'data-pin-id': p.id,
        ...(p.thread && String(p.thread).indexOf('tmp:') !== 0 ? {'data-thread': p.thread} : {}),
        onclick: function (e) { e.stopPropagation(); if (p.thread && String(p.thread).indexOf('tmp:') !== 0) T.open(p.thread, {detached: it.detached}); else editCanvasPin(it.i, e); }
      }, h('span', { text: String(pinNo(p, it.i)) }));
      c.layer.appendChild(T.paintPin(button, T.activityFor(p.thread, p)));
    });
    items.forEach(function (it) {
      var p = it.p;
      canvasLayer(it.sec).list.appendChild(h('li', null, h('button', { class: 'pin-row', onclick: function () { focusDzPin(it.i); } },
        h('span', { class: 'pin-num', text: String(pinNo(p, it.i)) }),
        h('span', { class: 'pin-body' }, h('span', { class: 'pin-where', text: (p.option ? p.option.toUpperCase() + ' · ' : '') + wlabel(p.w) }),
          h('span', { class: 'pin-text', text: p.text })))));
    });
    if (DOC && DOC.el) {
      T.canvasEntries().forEach(function (t) {
        if (!S.commentMode && !T.activityState(T.activityFor(t.id))) return;
        var sec = canvasSectionFor({block: t.anchor.block});
        if (!sec) return;
        var box = canvasBox(sec);
        var target = t.anchor && t.anchor.target ? cleanTarget(t.anchor.target) : null;
        if (target && target.quote) {
          var held = resolve(sec, target);
          var trange = held ? rangeFor(held, target) : null;
          if (trange) showCanvasRange(trange);
        }
        var s = T.spot(t, sec, box);
        var c = canvasLayer(sec);
        if (box) { c.layer.style.height = box.height + 'px'; c.layer.style.bottom = 'auto'; }
        var scroll = scrollPoint();
        var style = s.doc && box
          ? {left: num(s.doc.x - scroll.x - box.left, -1000000, 1000000) + 'px',
             top: num(s.doc.y - scroll.y - box.top, -1000000, 1000000) + 'px'}
          : {left: (s.x * 100) + '%', top: (s.y * 100) + '%'};
        c.layer.appendChild(T.marker(t, style, s.detached || t.detached));
        c.list.appendChild(T.row(t));
      });
    }
    CANVAS.forEach(function (c) { if (c.ghost) c.layer.appendChild(c.ghost); });   // a new pin's ghost survives the redraw
    updateSendbar();
    if (before !== null && before !== JSON.stringify(S.draft.pins)) saveDraft();
  }
  function editCanvasPin(i, e) {
    var p = S.draft.pins[i]; if (!p) return;
    var sec = canvasSectionFor(p);
    var title = sec ? (Element.prototype.getAttribute.call(sec, 'data-mr-title') || p.block) : p.block;
    notePop({ pin: p, title: 'Pin ' + (i + 1) + ' · ' + title,
      where: p.detached ? 'Element removed' : p.target ? 'on ' + p.target.label : wlabel(p.w) + ' · ~' + pct(p.x) + ', ' + pct(p.y),
      detached: p.detached, anchor: { x: e.clientX, y: e.clientY }, text: p.text,
      onSave: function (text) { p.text = text; if (p.thread) T.editThreadFirst(p.thread, text).catch(function () {}); saveDraft(); redrawCanvasPins(); },
      onDelete: function () { if (p.thread) T.deleteThreadFirst(p.thread).catch(function () {}); S.draft.pins.splice(i, 1); saveDraft(); redrawCanvasPins(); } });
  }
  var HOVER = { el: null, frame: 0, target: null, tab: null };
  function hoverMark(el) {   // a quote highlight wrapper is not an anchor target
    return isEl(el) && Element.prototype.matches.call(el, 'mark[data-mr-highlight]') ? el.parentElement : el;
  }
  function hoverLabel(el, sec) {
    if (!isEl(el) || !sec) return '';
    if (el === sec) return Element.prototype.getAttribute.call(sec, 'data-mr-title') || Element.prototype.getAttribute.call(sec, 'data-mr-ref') || '';
    var desc = describeTarget(el, sec);
    return desc ? desc.label : '';
  }
  function hideCanvasHover() {
    if (HOVER.frame) { cancelAnimationFrame(HOVER.frame); HOVER.frame = 0; }
    if (HOVER.el) HOVER.el.hidden = true;
  }
  function canvasHover(e) {
    if (!canComment() || !S.commentMode) { hideCanvasHover(); return; }
    HOVER.target = e.target;
    if (HOVER.frame) return;
    HOVER.frame = requestAnimationFrame(function () {
      HOVER.frame = 0;
      if (!HOVER.el) {
        var o = document.createElement('div');
        o.className = 'canvas-hover';
        o.hidden = true;
        o.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483646;outline:2px solid #6366f1;outline-offset:2px;border-radius:3px';
        var tab = document.createElement('span');
        tab.className = 'hl-tab';
        tab.style.cssText = 'position:absolute;left:-2px;top:-24px;max-width:320px;overflow:hidden;text-overflow:ellipsis;' +
          'white-space:nowrap;background:#18181b;color:#fff;font:600 11px/16px ' +
          '-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:1px 7px;border-radius:5px;pointer-events:none';
        o.appendChild(tab);
        (document.body || document.documentElement).appendChild(o);
        HOVER.el = o; HOVER.tab = tab;
      }
      var target = hoverMark(HOVER.target);
      if (!isEl(target) || closestOf(target, PIN_SKIP)) { HOVER.el.hidden = true; return; }
      var sec = sectionOf(target);
      if (!sec) { HOVER.el.hidden = true; return; }
      var el = target === sec ? sec : target;
      var r = rectOf(el);
      if (!r || !r.width || !r.height) { HOVER.el.hidden = true; return; }
      var label = hoverLabel(el, sec);
      HOVER.tab.textContent = label;
      HOVER.tab.hidden = !label;
      HOVER.tab.style.top = r.top > 28 ? '-24px' : 'auto';
      HOVER.tab.style.bottom = r.top > 28 ? 'auto' : '-24px';
      HOVER.el.hidden = false;
      HOVER.el.style.left = r.left + 'px';
      HOVER.el.style.top = r.top + 'px';
      HOVER.el.style.width = r.width + 'px';
      HOVER.el.style.height = r.height + 'px';
    });
  }
  function canvasClick(e) {
    if (!canComment() || !S.commentMode) return;
    var path = e.composedPath ? e.composedPath() : [e.target];
    for (var i = 0; i < path.length; i++) {
      var node = path[i];
      if (isEl(node) && Element.prototype.closest.call(node, PIN_SKIP)) return;
    }
    var sec = sectionOf(e.target);
    if (!sec) return;
    var ref = sectionRef(e.target);
    if (!ref) return;
    var title = Element.prototype.getAttribute.call(sec, 'data-mr-title') || ref;
    if (S.draft.pins.length >= LIMIT.pins) { toast('That’s ' + LIMIT.pins + ' pins — use the notes for the rest.', 'warn'); return; }
    var box = canvasBox(sec);
    if (!box || !box.width || !box.height) return;
    var x = num((e.clientX - box.left) / box.width, 0, 1), y = num((e.clientY - box.top) / box.height, 0, 1);
    var selection = document.getSelection ? document.getSelection() : null;
    var range = selection && !selection.isCollapsed && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
    if (range && !(Element.prototype.contains.call(sec, range.startContainer) && Element.prototype.contains.call(sec, range.endContainer))) range = null;
    var deep = isEl(path[0]) ? path[0] : e.target;
    if (isEl(deep) && Element.prototype.matches.call(deep, 'mark[data-mr-highlight]')) deep = deep.parentElement;
    var target = anchorFor(deep, sec, e.clientX, e.clientY, range);
    var scroll = scrollPoint();
    var doc = { x: num(e.clientX + scroll.x, 0, 1000000), y: num(e.clientY + scroll.y, 0, 1000000) };
    var ghost = h('div', { class: 'mpin ghost', style: { left: (x * 100) + '%', top: (y * 100) + '%' } }, h('span', { text: String(nextPinNo()) }));
    var c = canvasLayer(sec);
    c.ghost = ghost;
    c.layer.appendChild(ghost);
    notePop({ title: 'New pin · ' + (title || ref), where: target ? 'on ' + target.label : wlabel(S.width) + ' · ~' + pct(x) + ', ' + pct(y),
      anchor: { x: e.clientX, y: e.clientY }, isNew: true, target: target,
      onClose: function () { ghost.remove(); if (c.ghost === ghost) c.ghost = null; },
      onSave: function (text, ta) {
        saveNewThread({ id: newPinId(), block: ref, option: null, w: S.width, x: x, y: y, on: 'board', ...(target ? { target: target } : { doc: doc }) }, text, ta);
        redrawCanvasPins();
      } });
  }
  // A long page has no inner scroll, so a pin-list row must bring the pin itself into view by scrolling the board. The
  // pin's y is a fraction of the artifact page (the probe measured it against the page's full height) and, for a
  // long page, the iframe is that page at scale 1: page position = iframe top in document coordinates + y × page height.
  function artifactFrameFor(p) {
    return S.frames.find(function (x) { return x.ctx === 'artifact' && x.b.id === p.block && (x.canvas || x.device === p.w); }) || null;
  }
  function focusArtifactCanvas(p, thread) {
    if (!p || p.on === 'board') return null;
    var f = artifactFrameFor(p);
    if (!f || !f.canvas) return null;
    var z = f.view.zoom, x = f.el.clientWidth / 2, y = f.el.clientHeight / 2;
    var position = f.pinPositions.get(thread || p) || p;
    setArtifactView(f, x - num(position.x, 0, 1) * f.W * z, y - num(position.y, 0, 1) * f.pageH * z, z);
    var r = f.el.getBoundingClientRect(), bar = document.getElementById('dzbar');
    window.scrollBy({top: r.top - bar.getBoundingClientRect().bottom, behavior: 'instant'});
    r = f.el.getBoundingClientRect();
    return {right: r.left + x, top: r.top + y};
  }
  function longPinDocY(p) {
    var f = artifactFrameFor(p);
    if (!f || !f.long) return null;
    var r = rectOf(f.iframe);
    if (!r || !f.pageH) return null;
    return r.top + scrollPoint().y + num(p.y, 0, 1) * num(f.pageTrue || f.pageH, 0, 1000000);
  }
  function focusLongPin(p) {   // true when p is on a long page and the board was scrolled toward it
    var y = longPinDocY(p);
    if (y === null) return false;
    window.scrollTo({ top: Math.max(0, Math.round(y - window.innerHeight / 2)), behavior: 'smooth' });
    return true;
  }
  function focusDzPin(i) {
    var p = S.draft.pins[i]; if (!p) return;
    if (p.on !== 'board') {
      var box = focusArtifactCanvas(p);
      if (box) {
        if (p.thread) T.open(p.thread, {detached: p.detached, box: box});
        else editArtifactPin(artifactFrameFor(p), i, {x: box.right, y: box.top});
        return;
      }
    }
    if (p.w !== S.width && widths().indexOf(p.w) >= 0) { S.width = p.w; render(); setTimeout(function () { focusDzPin(i); }, 300); return; }
    if (p.on === 'board') {
      var sec = canvasSectionFor(p); if (!sec) return;
      sec.scrollIntoView({ behavior: 'smooth', block: 'center' });
      var bp = Element.prototype.querySelector.call(sec, '[data-mr-pin="' + i + '"]');
      if (bp) setTimeout(function () { bp.classList.add('flash'); setTimeout(function () { bp.classList.remove('flash'); }, 900); }, 350);
      return;
    }
    if (p.option && document.querySelector('#dz-' + p.block + ' .opts.one')) { S.show[p.block] = p.option; showOpt(p.block, p.option); }
    if (p.block === 'proto') { var fr = S.frames[0]; if (fr && fr.ctx === 'proto') try { fr.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'focus', i: i }, '*'); } catch (e) {} return; }
    var af = S.frames.filter(function (x) { return x.ctx === 'artifact' && x.b.id === p.block; })[0];
    if (af) {
      if (!focusLongPin(p)) af.iframe.scrollIntoView({ behavior: 'smooth', block: 'center' });
      try { af.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'focus', i: i }, '*'); } catch (e) {}
      return;
    }
    var m = MOCKS.filter(function (x) { return x.b.id === p.block && x.o.id === p.option; })[0]; if (!m) return;
    var pin = m.pins.querySelectorAll('.mpin')[[].slice.call(S.draft.pins).filter(function (q, j) { return j < i && q.block === p.block && q.option === p.option && q.w === p.w; }).length];
    (pin || m.outer).scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (pin) setTimeout(function () { pin.classList.add('flash'); setTimeout(function () { pin.classList.remove('flash'); }, 900); }, 350);
  }
  // LIVE (anchors.js) keeps its clicks in comment mode: a mock's own [data-mr-live] controls and the pin markers.
  // Events crossing the shadow root retarget to the host, so this reads the composed path.
  function mockLive(e) {
    var path = e.composedPath ? e.composedPath() : [e.target];
    return path.some(function (el) { return el instanceof Element && Element.prototype.closest.call(el, LIVE); });
  }
  function mockClick(m, e) {
    // Comment mode pins a click on anything in a mock, buttons, links, summaries and fields included, and cancels what the
    // click would have done (follow the link, toggle the summary, flip the checkbox, press the label). With comment mode
    // off every click is the mock's own.
    if (!canComment() || !S.commentMode || mockLive(e)) return;
    e.preventDefault();
    tapMock(m, e);
  }
  function tapMock(m, e) {
    if (!canComment() || !S.commentMode) return;
    var rc = m.inner.getBoundingClientRect();
    var x = num((e.clientX - rc.left) / rc.width, 0, 1), y = num((e.clientY - rc.top) / rc.height, 0, 1);
    if (S.draft.pins.length >= LIMIT.pins) { toast('That’s ' + LIMIT.pins + ' pins — use the notes for the rest.', 'warn'); return; }
    var selection = m.root.getRootNode().getSelection ? m.root.getRootNode().getSelection() : window.getSelection();
    var range = selection && !selection.isCollapsed && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
    if (range && !m.root.contains(range.commonAncestorContainer)) range = null;
    // A click inside a native control pins the control. Its press was cancelled, so a selection still showing is an older
    // one, not this click's.
    var hit = e.composedPath ? e.composedPath()[0] : e.target;
    if (closestOf(hit, CONTROL)) range = null;
    var target = e.mrTarget || anchorFor(controlOf(hit, m.root), m.root, e.clientX, e.clientY, range);
    var ghost = h('div', { class: 'mpin ghost', style: { left: (x * 100) + '%', top: (y * 100) + '%' } }, h('span', { text: String(nextPinNo()) }));
    m.ghost = ghost;
    m.pins.appendChild(ghost);
    notePop({ title: 'New pin · ' + pinWhat(m.b, m.o.id), where: target ? 'on ' + target.label : wlabel(S.width) + ' · ~' + pct(x) + ', ' + pct(y) + ' of the mock',
      anchor: { x: e.clientX, y: e.clientY }, text: '', isNew: true, target: target,
      onClose: function () { ghost.remove(); if (m.ghost === ghost) m.ghost = null; },
      onSave: function (text, ta) {
        saveNewThread({ id: newPinId(), block: m.b.id, option: m.o.id, w: S.width, x: x, y: y, ...(target ? {target: target} : {}) }, text, ta);
        redrawPins(m.b.id);
      } });
  }
  installAPI(function () { return MOCKS.map(function (m) { return m.root; }); }, function (target, request) {
    var node = request.range ? request.range.commonAncestorContainer : request.element;
    var m = MOCKS.find(function (m) { return m.root.contains(node); });
    if (!m) return;
    var r = request.range ? request.range.getBoundingClientRect() : rectOf(request.element);
    if (!r) return;
    tapMock(m, {clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
      target: request.element || node, composedPath: function () { return [request.element || node]; }, mrTarget: target});
  }, function () { return canComment() && S.commentMode; });
  function pinWhat(b, oid) { return (b.tag || b.id.toUpperCase()) + (oid ? ' · ' + oid.toUpperCase() : ''); }
  function editPin(m, i, e) {
    var p = S.draft.pins[i];
    notePop({ pin: p, title: 'Pin ' + (i + 1) + ' · ' + pinWhat(m.b, p.option), where: p.detached ? 'Element removed' : p.target ? 'on ' + p.target.label : wlabel(p.w) + ' · ~' + pct(p.x) + ', ' + pct(p.y),
      detached: p.detached, anchor: { x: e.clientX, y: e.clientY }, text: p.text,
      onSave: function (text) { p.text = text; if (p.thread) T.editThreadFirst(p.thread, text).catch(function () {}); saveDraft(); redrawPins(p.block); },
      onDelete: function () { if (p.thread) T.deleteThreadFirst(p.thread).catch(function () {}); S.draft.pins.splice(i, 1); saveDraft(); MOCKS.forEach(drawPins); redrawPins(p.block); } });
  }
  // generic pin popover (desktop: next to the tap; phone: bottom sheet)
  function notePop(o) {
    closePopover();
    var editable = canComment(), ta = null, chip = null, chipWrap = null;
    var body = editable ? (ta = h('textarea', { class: 'field', rows: '3', maxlength: String(LIMIT.comment), placeholder: 'What should change here?', 'aria-label': 'Pin comment' }))
      : h('p', { class: 'pop-text', text: o.text });
    if (ta) {
      ta.value = o.text || '';
      T.attachMentions(ta);
    }
    if (editable && o.target && o.target.quote) {
      var exact = o.target.quote.exact;
      chip = h('button', { class: 'pq-chip', type: 'button', title: 'Remove quote', 'aria-label': 'Remove quote'},
        h('span', { class: 'pq-text', text: exact }), h('span', { class: 'pq-x', 'aria-hidden': 'true', text: '×' }));
      chip.addEventListener('mousedown', function (e) { e.preventDefault(); });   // the textarea keeps focus
      chip.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        delete o.target.quote; delete o.target.position;
        if (chipWrap && chipWrap.parentNode) chipWrap.parentNode.removeChild(chipWrap);
        chip = null;
        var whereEl = el.querySelector('.pop-where');
        if (whereEl) whereEl.textContent = 'on ' + o.target.label;
        if (ta) ta.focus();
      });
      chipWrap = h('div', { class: 'pq' }, chip);
    }
    function save() {
      var text = T.meaningfulText(ta);
      if (!text) { ta.focus(); ta.classList.add('shake'); setTimeout(function () { ta.classList.remove('shake'); }, 400); return; }
      var cb = o.onSave; o.onClose = o.onClose || null; closePopover(); cb(text.slice(0, LIMIT.comment), ta);
    }
    var actions;
    if (editable && o.isNew) {
      var send = h('button', { class: 'btn btn-primary btn-round', type: 'button', 'aria-label': 'Add pin', title: 'Add pin',
        onclick: save }, icon('send'));
      actions = T.composerBar(ta, { cls: 'pop-actions composer-actions', send: send }).el;
    } else {
      actions = editable
        ? h('div', { class: 'pop-actions' }, o.onDelete ? h('button', { class: 'btn btn-ghost btn-danger', onclick: function () { var cb = o.onDelete; closePopover(); cb(); }, text: 'Delete' }) : null,
            h('span', { class: 'spacer' }), h('button', { class: 'btn btn-ghost', onclick: closePopover, text: 'Cancel' }),
            h('button', { class: 'btn btn-primary', onclick: save, text: o.isNew ? 'Add pin' : 'Save' }))
        : h('div', { class: 'pop-actions' }, h('span', { class: 'spacer' }), h('button', { class: 'btn btn-ghost', onclick: closePopover, text: 'Close' }));
    }
    var activity = o.pin ? h('div', {class: 'tp-typing pin-fix-detail'}) : null;
    var el = h('div', { class: 'popover', role: 'dialog', 'aria-label': o.title },
      h('div', { class: 'pop-head' }, h('span', { class: 'pin pin-static' }, icon('pin')),
        h('div', { class: 'pop-head-copy' }, h('strong', { text: o.title }), h('span', { class: 'pop-where', text: o.where })),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn btn-ghost icon-btn pop-close', type: 'button', 'aria-label': 'Close', title: 'Close',
          onclick: closePopover }, icon('close'))),
      o.detached ? h('div', {class: 'detached-state'}, h('span', {class: 'st warn', text: 'Element removed'}), h('div', {class: 'faint', text: 'Stays here and re-attaches if the element comes back.'})) : null,
      chipWrap, activity, body, actions);
    if (ta) ta.addEventListener('keydown', function (e) { if (e.defaultPrevented) return; if (e.key === 'Enter' && (e.metaKey || e.ctrlKey || !e.shiftKey)) { e.preventDefault(); save(); } });
    document.body.appendChild(el);
    pop = { el: el, f: null, onClose: function () {
      if (activity) T.statusRow(activity);
      if (o.onClose) o.onClose();
    } };
    if (activity) {
      pop.paintActivity = function () { T.statusRow(activity, T.activityFor(o.pin.thread, o.pin)); };
      pop.paintActivity();
    }
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
        onclick: function () { setMark(b.id, { status: mark(b.id).status === s ? null : s }); } },
        icon(s === 'approve' ? 'check' : s === 'change' ? 'pencil' : 'skip-forward'),
        h('span', { text: s === 'approve' ? 'Approve' : s === 'change' ? 'Change' : 'Skip' }));
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
    if (markEmpty(m)) delete S.draft.marks[id]; else S.draft.marks[id] = m;
    if (S.draft.page_marks) delete S.draft.page_marks[id];
    if (S.draft.page_notes) delete S.draft.page_notes[id];
    saveDraft(); updateSendbar(); updateNeedNote(id);
  }
  // A markable block's controls normally live inside #dz-<id>; a prototype's recap row lives in the docked footer.
  function markSurface(id) {
    var el = document.getElementById('dz-' + id);
    if (el && (el.hasAttribute('data-kind') || el.querySelector('.sbtn'))) return el;
    return document.querySelector('.decide[data-id="' + id + '"]') || el;
  }
  function updateNeedNote(id) {
    var el = markSurface(id); if (!el) return;
    var m = mark(id), need = CFG.require_note_on_change && isEditable() && m.status === 'change' && !(m.note || '').trim();
    el.classList.toggle('needs-note', !!need);
    var nn = el.querySelector('.need-note'); if (nn) nn.hidden = !need;
  }
  function refreshBlock(id) {
    var el = markSurface(id); if (!el) return;
    if (el.getAttribute('data-kind') === 'question') { refreshQuestion(el, id); redrawPins(id); return; }
    var m = mark(id);
    el.querySelectorAll('.sbtn').forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.s === m.status)); });
    el.querySelectorAll('.vbtn').forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.o === m.option)); });
    el.querySelectorAll('figure.opt').forEach(function (f) { f.classList.toggle('picked', f.dataset.o === m.option); });
    var st = el.querySelector('.status');
    if (st) {
      st.dataset.s = m.status || '';
      st.textContent = statusText(m);
    }
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
    var sec = h('section', { class: 'blk blk-else' }, h('h2', { text: 'Anything else?' }), ta);
    sec.setAttribute('data-mr-ref', 'else');
    sec.setAttribute('data-mr-title', 'Anything else');
    return sec;
  }

  // ---- prototype round: page mode — the clickable page fills the canvas edge to edge under the board header ----
  function renderProto(r) {
    var v = r.variants[0];
    var f = { r: r, vid: 'a', device: S.width, ctx: 'proto', W: 0, H: 0, pageH: 0, ghost: null, alive: false, scale: 1, stage: null };
    f.iframe = h('iframe', { src: frameUrl(r, v.file), title: R().title + ' prototype', sandbox: 'allow-scripts', referrerpolicy: 'no-referrer' });
    f.el = f.iframe;
    S.frames = [f];
    if (S.width !== 'desktop') f.iframe.style.width = (S.width === 'phone' ? CFG.mobile_width : CFG.tablet_width) + 'px';
    var stage = h('div', { class: 'proto w-' + S.width }, f.iframe);
    f.stage = stage;
    var wrap = h('div', { class: 'proto-wrap', id: 'dz-proto' }, stage, h('ol', { class: 'ipins', id: 'pins-proto' }));
    setTimeout(function () { refreshBlock('proto'); }, 0);
    document.body.classList.toggle('proto-pin', S.commentMode);
    return wrap;
  }
  function pushProto(f) {
    var out = [];
    S.draft.pins.forEach(function (p, i) { if (p.block === 'proto' && p.w === f.device && pinVisible(p)) out.push({ i: i, n: pinNo(p, i), x: p.x, y: p.y, hermes: T.activityState(T.activityFor(p.thread, p)), queued: T.queuedBehind(T.activityFor(p.thread, p)), ...(p.target ? {target: cleanTarget(p.target)} : {}) }); });
    if (f.ghost) out.push({ i: -1, n: nextPinNo(), x: f.ghost.x, y: f.ghost.y, ghost: true });
    T.protoEntries().forEach(function (t, j) {
      var a = t.anchor || {};
      out.push({ i: 60 + j, n: t.n, x: a.x || 0, y: a.y || 0, thread: t.id, detached: !!t.detached,
        hermes: T.activityState(T.activityFor(t.id)), queued: T.queuedBehind(T.activityFor(t.id)),
        ...(a.target ? {target: cleanTarget(a.target)} : {}) });
    });
    try { f.iframe.contentWindow.postMessage({ __mockreview: 1, cmd: 'state', comment: canComment() && S.commentMode, scale: 1, pins: pinsOn() ? out : [] }, '*'); } catch (e) {}
  }
  // The probe is untrusted: accept a computed colour and nothing else, so a forged message can never put a URL or a
  // declaration in an inline style.
  function probeColor(v) {
    if (typeof v !== 'string' || v.length > 48) return '';
    var s = v.trim();
    if (/^#[0-9a-f]{3,8}$/i.test(s)) return s;
    if (/^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\)$/i.test(s)) return s;
    return '';
  }
  function protoMessage(f, d) {
    var rc = f.iframe.getBoundingClientRect();
    if (d.type === 'hello') {
      f.alive = true;
      var c = probeColor(d.bg);
      if (c && f.stage) {   // the canvas around a page-mode frame is the page's own background
        f.stage.style.background = c;
        f.iframe.style.background = c;
      }
      var wrap = document.getElementById('dz-proto');
      if (wrap) wrap.setAttribute('data-ready', 'true');
      pushProto(f);
      return;
    }
    if (d.type === 'escape') { if (pop) closePopover(); else setCommentMode(false); return; }
    // A click that reaches the page while one of the board's own menus is open is an outside click for that menu:
    // dismiss it and do nothing else. Clicks inside the sandboxed frame never reach this document, so the
    // document-level outside-click listener cannot see them, and a composer must never open on top of the menu.
    if (S.exportMenu && (d.type === 'click' || d.type === 'pin')) { closeExportMenu(); return; }
    // The focus move that closed the menu arrived just before this click; it is the same gesture, not a new comment.
    if ((d.type === 'click' || d.type === 'pin') && S.frameDismiss) {
      var fresh = Date.now() - S.frameDismiss < 1000;
      S.frameDismiss = 0;
      if (fresh) { closeExportMenu(); return; }
    }
    if (d.type === 'positions') {
      (Array.isArray(d.pins) ? d.pins : []).slice(0, 60).forEach(function (q) {
        if (!q || !Number.isInteger(q.i)) return;
        var p = S.draft.pins[q.i]; if (!p || p.block !== 'proto' || p.w !== f.device || !p.target) return;
        if (!Number.isFinite(q.x) || !Number.isFinite(q.y)) return;
        var before = JSON.stringify(p);
        p.x = num(q.x, 0, 1); p.y = num(q.y, 0, 1); p.detached = !!q.detached;
        if (q.doc && Number.isFinite(q.doc.x) && Number.isFinite(q.doc.y)) p.target.doc = {x: num(q.doc.x, 0, 1000000), y: num(q.doc.y, 0, 1000000)};
        if (before !== JSON.stringify(p)) saveDraft();
      });
      return;
    }
    if (d.type === 'click') {
      if (typeof d.thread === 'string' && d.thread) { openFrameThread(f, 'proto', d.thread, d.detached); return; }
      if (!canComment() || !S.commentMode) return;
      if (S.draft.pins.length >= LIMIT.pins) { toast('That’s ' + LIMIT.pins + ' pins — use the notes for the rest.', 'warn'); return; }
      var x = num(d.x, 0, 1), y = num(d.y, 0, 1);
      var target = cleanTarget(d.target);
      var mine = f.ghost = { x: x, y: y }; pushProto(f);
      notePop({ title: 'New pin · prototype', where: target ? 'on ' + target.label : wlabel(f.device) + ' · ~' + pct(x) + ', ' + pct(y) + ' of the page', isNew: true, target: target,
        anchor: { x: rc.left + num(d.cx, 0, 4000), y: rc.top + num(d.cy, 0, 4000) }, text: '',
        onClose: function () { if (f.ghost === mine) { f.ghost = null; pushProto(f); } },
        onSave: function (text, ta) {
          saveNewThread({ block: 'proto', option: null, w: f.device, x: x, y: y, ...(target ? {target: target} : {}) }, text, ta);
          f.ghost = null; pushProto(f); redrawPins('proto');
        } });
    } else if (d.type === 'pin') {
      if (typeof d.thread === 'string' && d.thread) { openFrameThread(f, 'proto', d.thread, d.detached); return; }
      var i = num(d.i, -1, 99) | 0;
      if (i >= 60) {
        var st = T.protoEntries()[i - 60];
        if (st) openFrameThread(f, 'proto', st.id, st.detached);
        return;
      }
      var p = S.draft.pins[i]; if (!p) return;
      notePop({ title: 'Pin ' + (i + 1) + ' · prototype', detached: p.detached, where: p.detached ? 'Element removed' : p.target ? 'on ' + p.target.label : wlabel(p.w) + ' · ~' + pct(p.x) + ', ' + pct(p.y),
        anchor: { x: rc.left + num(d.cx, 0, 4000), y: rc.top + num(d.cy, 0, 4000) }, text: p.text,
        onSave: function (text) { p.text = text; if (p.thread) T.editThreadFirst(p.thread, text).catch(function () {}); saveDraft(); pushProto(f); redrawPins('proto'); },
        onDelete: function () { if (p.thread) T.deleteThreadFirst(p.thread).catch(function () {}); S.draft.pins.splice(i, 1); saveDraft(); pushProto(f); redrawPins('proto'); } });
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
      var decided = Object.keys(d.marks).filter(function (k) { return d.marks[k].status || d.marks[k].choice; }).length;
      var paged = Object.keys(d.page_state || {}).length;
      var filled = paged ? paged + ' page' + (paged === 1 ? '' : 's') + ' filled' : '';
      chips.push(h('span', { class: 'sum-pick' + (decided || paged ? '' : ' muted') }, decided || paged ? icon('check') : null,
        decided ? decided + ' marked' : paged ? filled : 'Nothing marked yet'));
      if (decided && paged) chips.push(h('span', { class: 'sum-count', text: filled }));
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
    updateGate();
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
      var sub = r.submission, ls = localSent(r.n), quiet = st.wake === false;
      var title, subline, cls;
      var built = fb.action === 'build', glabel = (r.gate && r.gate.label) || 'Build';
      if (sub && sub.status === 'notified') {
        if (sub.stalled_notice_at) {
          title = 'Hermes may have stalled'; cls = 'warn';
          subline = 'Sent ' + fmtDate(sub.submitted_at) + (sub.amended ? ' (amended)' : '') + '. No new round yet — ask in the chat or resend from the board.';
        } else if (quiet) {
          title = 'Saved'; cls = 'ok'; subline = savedCopy(st, '');
        } else {
          title = built ? glabel + ' pressed ✓' : 'Sent ✓ — Hermes is on it'; cls = 'ok';
          subline = 'Sent ' + fmtDate(sub.submitted_at) + (sub.amended ? ' (amended)' : '') + ' · Hermes was notified and is working on it.';
        }
      } else if (sub && sub.status === 'stored') {
        title = 'Saved'; cls = 'ok'; subline = savedCopy(st, STORED_GUEST_COPY, sub);
      } else if (sub && sub.status === 'pending_notify') {
        title = 'Saved ✓ — still reaching Hermes'; cls = 'warn';
        subline = 'Your feedback is stored. Hermes hasn’t confirmed yet; the board server retries every minute.';
      } else if (ls) {
        title = quiet ? 'Saved' : built ? glabel + ' pressed ✓' : 'Sent ✓ — Hermes is on it'; cls = 'ok';
        subline = quiet ? savedCopy(st, '') : 'Sent ' + fmtDate(ls.at) + '.';
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
      if (ownerSends()) inner.appendChild(h('button', { class: 'btn', onclick: startAmend, text: 'Amend feedback' }));
      return;
    }
    if (!ownerSends()) {   // a reviewer who is not the owner keeps editing locally; Send and Build are not theirs
      sendbar.classList.add('sb-reader');
      inner.appendChild(h('div', { class: 'sb-summary' }, summaryChips()));
      inner.appendChild(h('p', { class: 'sb-owner-only', text: OWNER_ONLY_COPY }));
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
    // A gate round has ONE footer button, picked by footer_button: Send, or Build. Build stays in the slot, locked with
    // the reason in its tooltip, while blocks are still open; a block on Change means this round cannot be built, so the
    // slot turns into Send for those notes.
    var gated = isNew(r) && !!r.gate && !S.amending && isEditable() && footerButton() === 'build';
    var gs = gated ? gateState(r.decisions, S.draft.marks, r.gate) : null;
    if (gated && !gs.changed.length) {
      var ok = gs.ready && !S.sending;
      inner.appendChild(h('button', { class: 'btn btn-primary btn-send btn-gate' + (ok ? '' : ' is-disabled'), id: 'gate-btn',
        'aria-disabled': ok ? 'false' : 'true', title: gateTitle(r.gate, gs, true),
        onclick: function () { if (this.getAttribute('aria-disabled') !== 'true') send('build'); else toast(this.getAttribute('title'), 'warn'); } },   // no hover on a phone: a tap says why
        icon(gateIcon(gs.ready, true)), h('span', { class: 'gate-label', text: S.sending ? 'Sending…' : GATE_BTN })));
      return;
    }
    inner.appendChild(h('button', {
      class: 'btn btn-send btn-primary', disabled: ready ? null : true, onclick: function () { send(); },
      title: missing.length ? 'Every Change needs a note first' : hasContent(S.draft) ? 'Send this round’s feedback to Hermes' : (isNew(r) ? 'Mark a decision, use the page controls, drop a pin or write a note first' : 'Pick a variant, drop a pin or write a note first')
    }, S.sending ? h('span', { class: 'spinner' }) : icon('send'), S.sending ? 'Sending…' : (S.amending ? 'Send amendment' : isNew(r) ? 'Send' : 'Send to Hermes')));
  }

  function toast(msg, kind) {
    var t = h('div', { class: 'toast toast-' + (kind || 'info'), role: 'status', text: msg });
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add('out'); }, 3200);
    setTimeout(function () { t.remove(); }, 3700);
  }

  // ---------- actions ----------
  function switchRound(n) {
    closeFull(); S.show = {};
    S.view = n; S.amending = false; S.error = null;
    history.replaceState(null, '', n === R().current_round ? location.pathname : '#round-' + n);
    T.close();
    loadDraft(); render(); window.scrollTo({ top: 0 });
    T.setView();
  }
  function startAmend() { S.amending = true; S.error = null; loadDraft(); render(); }
  function cancelAmend() { S.amending = false; S.error = null; lsDel(key('amend')); loadDraft(); render(); }

  function send(action) {
    var build = action === 'build';
    if (build) {   // the gate's own rule, the same one the server enforces
      if (S.sending || !isEditable() || !gateState(viewed().decisions, S.draft.marks, viewed().gate).ready) { updateGate(); return; }
    } else if (S.sending || !hasContent(S.draft)) return;
    if (!build && missingNotes().length) { updateSendbar(); return; }
    if (S.draft.choice === 'none' && !S.draft.choice_note.trim() && !S.draft.guidelines.trim()) {
      S.error = { title: 'Say what to combine', text: 'You chose “None / combine” — add a line on what to take from which variant.' };
      updateSendbar();
      var f = document.getElementById('f-choice_note'); if (f) { f.focus(); f.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
      return;
    }
    S.error = null; S.sending = true; updateSendbar();
    var r = viewed(), amend = S.amending, body = payload(S.draft, build ? 'build' : (isNew(r) && r.gate ? 'send' : undefined));
    var p = sendLocal(r, body, amend);
    p.then(function (res) {
      S.sending = false;
      lsSet(key('sent', r.n), { at: new Date().toISOString(), amend: amend, feedback: body });
      lsDel(key('draft', r.n)); lsDel(key('amend', r.n));
      S.amending = false;
      if (res && res.state) applyState(res.state);
      loadDraft(); render();
      // Saved without a wake: a wake-off board, or a Send the request gate held (from Funnel, say) with its reason.
      var quiet = R().wake === false, held = !quiet && !!res && res.status === 'stored';
      toast(quiet ? savedCopy(R(), '') : held ? res.detail || savedCopy(R(), STORED_GUEST_COPY) : res && res.notified === false ? 'Saved — Hermes will be pinged as soon as it answers.' : build ? ((r.gate && r.gate.label) || 'Build') + ' pressed ✓' : 'Sent ✓ — Hermes is on it', quiet ? 'ok' : held || (res && res.notified === false) ? 'warn' : 'ok');
      maybePollForNotify();
      maybeWatchForRound();
    }).catch(function (e) {
      S.sending = false;
      S.error = { title: e.title || 'Couldn’t send', text: e.message || String(e) };
      updateSendbar();
      if (e.refresh) fetchState().then(function (st) { applyState(st); updateSendbar(); }).catch(function () {});
    });
  }

  function err(title, message, refresh) { var e = new Error(message); e.title = title; e.refresh = refresh; return e; }

  function sendLocal(r, body, amend) {
    return postJSON('api/submit', { round: r.n, token: R().token, amend: amend, feedback: body }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (res.status === 409) throw err('Already sent', j.error || 'This round already has feedback.', true);
        if (!res.ok) throw err('Couldn’t send', j.error || ('Server answered HTTP ' + res.status));
        return fetchState().then(function (st) { return { notified: j.notified, status: j.status, detail: j.detail, state: st }; });
      });
    }, function () { throw err('Offline?', 'The board server didn’t answer. Your draft is saved on this device — try again in a moment.'); });
  }

  // After a submit the board server records the wake-up; poll for the notified state.
  function maybePollForNotify() {
    clearTimeout(S.pollTimer);
    var st = R(), r = cur();
    var ls = localSent(r.n);
    var waiting = (r.submission && r.submission.status === 'pending_notify') ||
      (ls && r.submission && ls.amend && Date.parse(r.submission.submitted_at) < Date.parse(ls.at) - 60000);
    if (!waiting || st.status !== 'open') return;
    var tries = 0;
    (function tick() {
      S.pollTimer = setTimeout(function () {
        tries++;
        fetchState().then(function (fresh) {
          var before = JSON.stringify(cur().submission);
          applyState(fresh);
          if (JSON.stringify(cur().submission) !== before && S.view === fresh.current_round && !S.amending) updateSendbar();
          var sub = cur().submission;
          if (sub && sub.status === 'notified') maybeWatchForRound();
          else if (tries < 30) tick();
        }).catch(function () { if (tries < 30) tick(); });
      }, 15000);
    })();
  }

  // Slow watch: while the current round is sent and no newer round exists, look at state.json every 30 s (for up to
  // 2 h, 8 h after Build, and not once the review closes) so the stalled hint and the next round show up
  // without a reload.
  var WATCH_EVERY = 30000, WATCH_FOR = 2 * 3600 * 1000, WATCH_FOR_BUILD = 8 * 3600 * 1000;
  function watchWindowMs(r) {   // a build may take stale_after_* x build_stale_factor (60 / 240 min by default)
    var fb = submittedFeedback(r);
    return fb && fb.action === 'build' ? WATCH_FOR_BUILD : WATCH_FOR;
  }

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
    var round = r.n, until = Date.now() + watchWindowMs(r);
    (function tick() {
      S.watchTimer = setTimeout(function () {
        if (Date.now() > until) return;
        fetchState().then(function (fresh) {
          var before = cur().submission, hadStalled = !!(before && before.stalled_notice_at);
          applyState(fresh);
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

  // ---------- Export menu: PNG, PDF, HTML and Markdown, with or without comments ----------
  function exportWidth() {
    if (S.width === 'phone') return +CFG.mobile_width || 390;
    if (S.width === 'tablet') return +CFG.tablet_width || 820;
    return 1280;
  }
  function exportQuery(fmt) {
    var rid = R().id, n = viewed().n, c = S.exportComments ? '1' : '0';
    if (fmt === 'png') return '/r/' + rid + '/export/capture?round=' + n + '&width=' + exportWidth();
    if (fmt === 'pdf') return '/r/' + rid + '/export/print?round=' + n + '&comments=' + c;
    return '/r/' + rid + '/export.html?round=' + n + '&comments=' + c + '&format=' + (fmt === 'markdown' ? 'md' : 'html');
  }
  function downloadMarkdown(name, text) {
    var blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    var href = URL.createObjectURL(blob);
    var a = h('a', { href: href, download: name });
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(href); a.remove(); }, 1000);
  }
  function exportMarkdown() {
    fetch(exportQuery('markdown'), { cache: 'no-store', credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.text();
    }).then(function (html) {
      if (typeof TurndownService === 'undefined') throw new Error('the Markdown converter did not load');
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var name = Element.prototype.getAttribute.call(doc.body, 'data-export-name') || 'review-export';
      var md = new TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced' }).turndown(doc.body);
      downloadMarkdown(name + '.md', md);
    }).catch(function (e) { toast('Export: ' + String((e && e.message) || e), 'warn'); });
  }
  function runExport(fmt) {
    if (fmt === 'markdown') return exportMarkdown();
    var url = exportQuery(fmt);
    if (fmt === 'html') { location.href = url; return; }
    window.open(url, '_blank', 'noopener');
  }
  function menuFocusables(menu) {
    return Array.prototype.filter.call(
      menu.querySelectorAll('[role="menuitem"],[role="menuitemcheckbox"],[role="menuitemradio"],[role="radio"],a[href],button:not([role]),input,select'),
      function (el) { return !el.disabled && el.offsetParent !== null; });
  }
  function positionMenu(menu, btn) {
    var r = btn.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    var w = menu.offsetWidth, hh = menu.offsetHeight;
    var left = Math.min(Math.max(8, r.right - w), vw - w - 8);
    var top = r.bottom + 4;
    if (top + hh > vh - 8) top = Math.max(8, r.top - hh - 4);
    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(top) + 'px';
  }
  // Every board menu (More, the phone round picker) lives in one slot, so Escape, an outside click and the
  // prototype's Export-input guard all see exactly one open menu.
  function closeExportMenu() {
    var st = S.exportMenu;
    if (!st) return;
    S.exportMenu = null;
    Element.prototype.setAttribute.call(st.btn, 'aria-expanded', 'false');
    var liveTools = st.el && st.el.querySelector('.tacts');
    var holder = document.querySelector('.header-tools');
    if (liveTools && holder && holder.isConnected) holder.appendChild(liveTools);
    if (st.el && st.el.parentNode) st.el.parentNode.removeChild(st.el);
    if (st.backdrop && st.backdrop.parentNode) st.backdrop.parentNode.removeChild(st.backdrop);
    document.body.classList.remove('overlay-open');
    if (st.outside) document.removeEventListener('click', st.outside, true);
    if (st.key) document.removeEventListener('keydown', st.key, true);
    if (st.syncFrame) {
      window.removeEventListener('scroll', st.syncFrame, true);
      window.removeEventListener('resize', st.syncFrame);
    }
    if (st.frameFocus) document.removeEventListener('focusin', st.frameFocus, true);
    if (st.frameBlur) window.removeEventListener('blur', st.frameBlur);
    if (st.shields && st.shields.length) {
      // Keep them for this event tick: a pointerdown here removes the shield before the browser dispatches the
      // click, and a click retargeted into the frame must not become a comment. The next task removes them.
      var shields = st.shields;
      st.shields = [];
      setTimeout(function () {
        shields.forEach(function (sh) { if (sh.parentNode) sh.parentNode.removeChild(sh); });
      }, 0);
    }
    if (st.btn && st.btn.isConnected) st.btn.focus({ preventScroll: true });
  }
  // A focus move into a frame dismisses the open popup like an outside click. The click that moved the focus may
  // still arrive as the frame's own postMessage, so remember the moment and swallow that same gesture below.
  function dismissFramePopup() {
    if (!S.exportMenu) return;
    S.frameDismiss = Date.now();
    closeExportMenu();
  }
  function openMenuAt(btn, build, opts) {
    closeExportMenu();
    S.frameDismiss = 0;
    opts = opts || {};
    var phone = window.innerWidth <= 600;
    var sheet = !!opts.sheet && phone;
    var menu = h('div', { class: 'menu ' + (opts.exportMenu ? 'export-menu ' : '') + (sheet ? 'menu-sheet' : 'menu-pop'), role: 'menu',
      'aria-label': opts.label || 'Menu' });
    build(menu);
    var backdrop = null;
    if (sheet) {
      backdrop = h('div', { class: 'menu-backdrop' });
      document.body.appendChild(backdrop);
      document.body.appendChild(menu);
      document.body.classList.add('overlay-open');
    } else {
      document.body.appendChild(menu);
      positionMenu(menu, btn);
    }
    Element.prototype.setAttribute.call(btn, 'aria-expanded', 'true');
    var st = { el: menu, menu: menu, btn: btn, backdrop: backdrop, sheet: sheet, shields: [] };
    st.outside = function (e) { if (!isEl(e.target)) return; if (!menu.contains(e.target) && !btn.contains(e.target)) closeExportMenu(); };
    st.key = function (e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeExportMenu(); return; }
      if (e.key === 'Tab') {
        var f = menuFocusables(menu); if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
        return;
      }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(e.key) < 0 || typing(e.target)) return;
      var items = menuFocusables(menu); if (!items.length) return;
      e.preventDefault();
      var i = items.indexOf(document.activeElement);
      if (e.key === 'Home') i = 0;
      else if (e.key === 'End') i = items.length - 1;
      else if (e.key === 'ArrowDown') i = (i + 1) % items.length;
      else i = (i - 1 + items.length) % items.length;
      items[i].focus();
    };
    S.exportMenu = st;
    // A click into a sandboxed frame is an outside click, but that frame's own document gets the event, never
    // this one. While a menu is open, a transparent shield over every frame makes the pointer land here instead:
    // the existing outside-click listener then dismisses the menu and the page sees no comment tap. The shield is
    // rebuilt on scroll/resize so it keeps covering the frame, and a focus move into a frame closes the menu too.
    st.syncFrame = function () {
      st.shields.forEach(function (sh) { if (sh.parentNode) sh.parentNode.removeChild(sh); });
      st.shields = [];
      if (S.exportMenu !== st) return;
      Array.prototype.forEach.call(document.querySelectorAll('iframe'), function (f) {
        var r = f.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        var sh = h('div', { class: 'frame-shield', 'aria-hidden': 'true' });
        sh.style.left = Math.round(r.left) + 'px';
        sh.style.top = Math.round(r.top) + 'px';
        sh.style.width = Math.round(r.width) + 'px';
        sh.style.height = Math.round(r.height) + 'px';
        sh.addEventListener('pointerdown', function (e) {
          if (e.cancelable) e.preventDefault();
          closeExportMenu();
        }, true);
        st.shields.push(sh);
        document.body.appendChild(sh);
      });
    };
    st.frameFocus = function (e) {
      if (e.target && e.target.tagName === 'IFRAME') dismissFramePopup();
    };
    st.frameBlur = function () {
      var a = document.activeElement;
      if (a && a.tagName === 'IFRAME') dismissFramePopup();
    };
    st.syncFrame();
    window.addEventListener('scroll', st.syncFrame, true);
    window.addEventListener('resize', st.syncFrame);
    document.addEventListener('focusin', st.frameFocus, true);
    window.addEventListener('blur', st.frameBlur);
    document.addEventListener('click', st.outside, true);
    document.addEventListener('keydown', st.key, true);
    if (backdrop) backdrop.addEventListener('click', closeExportMenu);
    requestAnimationFrame(function () {
      var f = menuFocusables(menu);
      if (f.length) f[0].focus({ preventScroll: true });
    });
  }
  function exportItem(fmt, label, hint, iconName) {
    return h('button', {
      class: 'menu-item export-item', type: 'button', role: 'menuitem', 'data-format': fmt,
      title: hint, onclick: function () { closeExportMenu(); runExport(fmt); }
    }, icon(iconName || 'download'), h('span', { text: label }));
  }
  function exportCommentsItem() {
    var check = h('span', { class: 'switch' + (S.exportComments ? ' on' : ''), 'aria-hidden': 'true' },
      h('span', { class: 'switch-knob' }));
    var toggle = h('button', {
      class: 'menu-item export-comments', type: 'button', role: 'menuitemcheckbox',
      'aria-checked': S.exportComments ? 'true' : 'false',
      onclick: function (e) {
        e.preventDefault();
        S.exportComments = !S.exportComments;
        setPref('export:comments', S.exportComments ? '1' : '0');
        Element.prototype.setAttribute.call(toggle, 'aria-checked', S.exportComments ? 'true' : 'false');
        check.className = 'switch' + (S.exportComments ? ' on' : '');
      }
    }, icon('comments'), h('span', { class: 'menu-grow', text: 'Include comments' }), check);
    return toggle;
  }
  function themeChoices(r) {
    var d = r && r.design;
    if (d && d.themes && Object.keys(d.themes).length > 1) {
      return Object.keys(d.themes).map(function (t) { return { value: t, label: t.charAt(0).toUpperCase() + t.slice(1) }; });
    }
    return [{ value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }, { value: 'auto', label: 'Auto' }];
  }
  function setBoardTheme(r, value) {
    if (r && r.design) setPref('dtheme:' + R().id + ':' + r.design.name, value);
    else setPref('theme:' + R().id, value);
    // Chrome tokens alone are not enough: every mock root carries its own data-theme and background, and the
    // approved base refreshed them by re-rendering. keepPlace holds the reading position; render() closes the
    // menu, so put focus back on the new More button instead of letting it fall to <body>.
    keepPlace(render);
    var more = document.querySelector('.more-open');
    if (more) more.focus({ preventScroll: true });
  }
  function moreButton() {
    return h('button', {
      class: 'icon-btn more-open export-open', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      title: 'More', 'aria-label': 'More actions',
      onclick: function (e) {
        e.preventDefault(); e.stopPropagation();
        if (S.exportMenu) { closeExportMenu(); return; }
        openMenuAt(this, moreMenu, { label: 'Review menu', sheet: true, exportMenu: true });
      }
    }, icon('more'));
  }
  function moreMenu(menu) {
    var r = viewed(), st = R();
    // Comments and Activity open a modal overlay. Close this menu (and its phone backdrop) in the capture phase:
    // the event path is already fixed, so the moved .tacts button still runs its own handler and opens the panel,
    // but the panel never opens underneath a menu that still owns focus and the backdrop.
    menu.addEventListener('click', function (e) {
      if (closestOf(e.target, '.tcomments, .tactivity')) closeExportMenu();
    }, true);
    if (CFG.show_theme_toggle) {
      var choices = themeChoices(r), value = r.design ? designTheme(r) : boardTheme();
      if (choices.length > 1) {
        menu.appendChild(h('div', { class: 'menu-label', text: 'Appearance' }));
        var tseg = segWithValues(choices, value, function (v) { setBoardTheme(r, v); segSync(tseg, v); }, 'Theme');
        tseg.classList.add('sm', 'menu-seg');
        menu.appendChild(h('div', { class: 'menu-row' }, icon('palette'),
          h('span', { class: 'menu-grow', text: 'Theme' }), tseg));
      }
    }
    if (window.innerWidth <= 600 && widths().length > 1) {
      var wseg = segWithValues(widths().map(function (w) { return { value: w, label: wlabel(w), icon: WICON[w] }; }),
        S.width, function (v) { if (v !== S.width) { closeExportMenu(); S.width = v; keepPlace(render); } }, 'Mock width');
      wseg.classList.add('sm', 'menu-seg', 'wseg');
      menu.appendChild(h('div', { class: 'menu-row' }, icon('desktop'),
        h('span', { class: 'menu-grow', text: 'View' }), wseg));
    }
    var holder = document.querySelector('.header-tools');
    var tools = holder && holder.querySelector('.tacts');
    if (tools) holder.removeChild(tools); else tools = T.headerActions();
    menu.appendChild(h('div', { class: 'menu-label', text: 'Review' }));
    menu.appendChild(h('div', { class: 'menu-tools' }, tools));
    if (canDo('manage')) {
      menu.appendChild(h('button', { class: 'menu-item share-menu', type: 'button', role: 'menuitem',
        onclick: function () { closeExportMenu(); openShare(); } }, icon('share'), h('span', { text: 'Share' })));
    }
    if (canDo('read')) {
      menu.appendChild(h('div', { class: 'menu-sep', role: 'separator' }));
      menu.appendChild(h('div', { class: 'menu-label', text: 'Export' }));
      menu.appendChild(exportItem('png', 'Download as PNG', 'Download this round as an image', 'download'));
      menu.appendChild(exportItem('pdf', 'Download as PDF', 'Open the print page and print or save it as a PDF', 'file-text'));
      menu.appendChild(exportItem('html', 'Download as HTML', 'Download one self-contained HTML page', 'download'));
      menu.appendChild(exportItem('markdown', 'Download as Markdown', 'Download the same document as Markdown', 'file-text'));
      menu.appendChild(exportCommentsItem());
    }
    menu.appendChild(h('div', { class: 'menu-sep', role: 'separator' }));
    menu.appendChild(h('div', { class: 'menu-row menu-info' }, h('span', { class: 'menu-grow', text: 'Hosted on' }), modeBadge(st, false),
      h('span', { class: 'badge', text: st.mode === 'public' ? 'Public' : 'Private' })));
    var settings = settingsMenuItem(), signout = signOutMenuItem(), guest = guestNameEditor();
    if (settings) menu.appendChild(settings);
    if (signout) menu.appendChild(signout);
    if (guest) menu.appendChild(guest);
  }

  var T = createThreads({
    view: function () { return S.view; },
    width: function () { return S.width; },
    csrf: csrfToken,
    toast: toast,
    canSendToHermes: function () { return !!R() && R().wake !== false; },
    setView: function (n) { if (R() && S.view !== n) switchRound(n); },
    pinRequests: pinRequests, pinNow: serverNow, paintPinActivity: paintPinActivity,
    blockLabel: blockLabel, openPinFix: openPinFix,
    refreshPinActivity: function () { refreshPinActivity(); },
    reloadState: function () { refreshPinActivity(true); },
    mirrored: draftThreadIds,
    hideResolved: function () { return !!CFG.hide_resolved_pins; },
    firstOption: function (block) {
      var v = viewed(), dec = v && v.decisions;
      var b = dec && (dec.blocks || []).filter(function (x) { return String(x.id) === String(block); })[0];
      return b && b.options && b.options.length ? firstShown(b) : null;
    },
    redraw: redrawThreadSurfaces
  });
  // ---------- Share dialog: people, one link, seats and the version picker ----------
  var SHARE_ROLES = [['reviewer', 'Reviewer'], ['commenter', 'Commenter'], ['viewer', 'Viewer']];

  function shareSelect(cls, options, value, onchange, label) {
    var sel = h('select', { class: 'share-select ' + cls, 'aria-label': label });
    options.forEach(function (o) { sel.appendChild(h('option', { value: o[0] }, o[1])); });
    sel.value = String(value);
    sel.addEventListener('change', onchange);
    return sel;
  }

  // A reviewer can wake Hermes with an @Hermes mention (wakeguard.request_gate), so giving someone Reviewer takes one
  // more, explicit step. Every role select in the Share dialog (the link's guests, a person, a new invite) is this one.
  var REVIEWER_CONFIRM_BODY = 'People with the Reviewer role can wake Hermes with an @Hermes mention on boards that ' +
    'wake a messaging chat. Hermes then runs in that chat with mock-review’s own tools, which reach every review on ' +
    'this install, so a Reviewer can read and reply on your other reviews: give Reviewer only to people you trust ' +
    'with all of them. They can’t press Send or Build.';
  function roleSelect(cls, value, onchange, label) {
    var prev = String(value);
    var sel = shareSelect(cls, SHARE_ROLES, value, function (e) {
      var was = prev;
      if (sel.value !== 'reviewer' || was === 'reviewer') { prev = sel.value; onchange(e); return; }
      confirmReviewer(sel, function () { prev = sel.value; onchange(e); }, function () { sel.value = was; });
    }, label);
    return sel;
  }

  // Cancel, Escape and a click on the backdrop put the old role back and send nothing; the Share dialog underneath
  // stays open (its own key handler stands aside while S.share.confirm is set).
  function confirmReviewer(sel, yes, no) {
    var cancel = h('button', { class: 'btn role-confirm-cancel', type: 'button', text: 'Cancel' });
    var ok = h('button', { class: 'btn btn-primary role-confirm-ok', type: 'button', text: 'Give Reviewer access' });
    var box = h('div', { class: 'role-confirm', role: 'dialog', 'aria-modal': 'true',
                         'aria-labelledby': 'role-confirm-title', 'aria-describedby': 'role-confirm-body' },
      h('h2', { class: 'role-confirm-title', id: 'role-confirm-title', text: 'Give Reviewer access?' }),
      h('p', { class: 'role-confirm-body', id: 'role-confirm-body', text: REVIEWER_CONFIRM_BODY }),
      h('div', { class: 'role-confirm-actions' }, cancel, ok));
    var backdrop = h('div', { class: 'role-confirm-backdrop' }, box);
    var share = S.share, done = false;
    function close(confirmed) {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', key, true);
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      if (share && share.confirm === backdrop) share.confirm = null;
      if (confirmed) yes(); else no();
      if (sel.isConnected) sel.focus({ preventScroll: true });
    }
    function key(e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); return; }
      if (e.key !== 'Tab') return;
      e.preventDefault();   // two buttons: Tab and Shift+Tab move between them, never behind the dialog
      (document.activeElement === cancel ? ok : cancel).focus();
    }
    cancel.addEventListener('click', function () { close(false); });
    ok.addEventListener('click', function () { close(true); });
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) close(false); });
    if (share) share.confirm = backdrop;
    document.addEventListener('keydown', key, true);
    document.body.appendChild(backdrop);
    cancel.focus({ preventScroll: true });
  }

  function shareExpiryValue(link) {
    if (!link || link.expires_at == null) return 'never';
    return (link.expires_at * 1000) - Date.now() <= 26 * 3600 * 1000 ? '24h' : '7d';
  }

  function closeShare() {
    if (!S.share) return;
    var st = S.share;
    S.share = null;
    var liveTools = st.el && st.el.querySelector('.tacts');
    var holder = document.querySelector('.header-tools');
    if (liveTools && holder && holder.isConnected) holder.appendChild(liveTools);
    if (st.el && st.el.parentNode) st.el.parentNode.removeChild(st.el);
    document.body.classList.remove('modal-open');
    if (st.key) document.removeEventListener('keydown', st.key, true);
    if (st.returnTo && st.returnTo.isConnected) st.returnTo.focus({ preventScroll: true });
  }

  function shareLinkRow(url, label) {
    var code = h('code', { class: 'share-url', title: url, text: url });
    var btn = h('button', { class: 'btn share-copy', type: 'button', text: 'Copy' });
    btn.addEventListener('click', function () { copyText(url, btn, code); });
    return h('div', { class: 'share-row share-link-row', 'aria-label': label }, code, btn);
  }

  function copyText(text, btn, target) {
    function done() { btn.textContent = 'Copied'; }
    function manual() {
      try {
        var range = document.createRange();
        range.selectNodeContents(target);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (e) {}
      btn.textContent = 'Press \u2318C';
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, manual);
    } else {
      manual();
    }
  }

  function sharePost(body) {
    return postJSON('api/share', body).then(function (res) {
      return res.json();
    }).then(function (view) {
      if (!view || !view.ok) throw new Error((view && view.error) || 'the share link was not changed');
      if (S.share) { S.share.view = view; renderShare(view); }
      return view;
    }).catch(function (e) {
      if (S.share && S.share.box) {
        Array.prototype.forEach.call(S.share.box.querySelectorAll('button'), function (b) {
          b.disabled = false;
          if (b.dataset.confirm === '1') {
            delete b.dataset.confirm;
            b.textContent = b.textContent.replace(/\?$/, '');
          }
        });
      }
      toast('Share: ' + String(e.message || e), 'warn');
    });
  }

  function peoplePost(body) {
    return postJSON('api/people', body).then(function (res) {
      return res.json();
    }).then(function (res) {
      if (!res || !res.ok) throw new Error((res && res.error) || 'the people list was not changed');
      if (!S.share) return res;
      if (res.invite) S.share.invite = res.invite;
      else if (res.people && S.share.view) S.share.view.people = res.people;
      renderShare(S.share.view);
      return res;
    }).catch(function (e) { toast('Share: ' + String(e.message || e), 'warn'); });
  }

  function openShare() {
    if (S.share) return;
    var backdrop = h('div', { class: 'share-backdrop' });
    var box = h('div', { class: 'share-dialog', role: 'dialog', 'aria-modal': 'true',
                         'aria-label': 'Share ' + (R() ? R().title : '') });
    backdrop.appendChild(box);
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) closeShare(); });
    document.body.appendChild(backdrop);
    document.body.classList.add('modal-open');
    S.share = { el: backdrop, box: box, view: null, invite: null, inviteRole: 'commenter', inviteExpiry: 'never',
      returnTo: document.activeElement };
    S.share.key = function (e) {
      if (!S.share || S.share.confirm) return;   // "Give Reviewer access?" is open on top and handles its own keys
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeShare(); return; }
      if (e.key !== 'Tab') return;
      var f = Array.prototype.filter.call(box.querySelectorAll('button,input,select,a[href],textarea'), function (el) {
        return !el.disabled && el.offsetParent !== null;
      });
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    };
    document.addEventListener('keydown', S.share.key, true);
    box.appendChild(h('p', { class: 'share-faint', text: 'Loading the share settings\u2026' }));
    fetch('api/share', { cache: 'no-store', credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (view) {
      if (!S.share) return;
      S.share.view = view;
      renderShare(view, true);
    }).catch(function (e) {
      if (!S.share) return;
      box.textContent = '';
      box.appendChild(h('p', { class: 'share-faint', text: 'Could not load the share settings: ' + String(e.message || e) }));
    });
  }

  function renderShare(view, focus) {
    if (!S.share || !view) return;
    var box = S.share.box, link = view.link;
    box.textContent = '';
    box.appendChild(h('div', { class: 'share-head' },
      h('h2', { class: 'share-title', text: 'Share \u201c' + R().title + '\u201d' }),
      h('button', { class: 'btn btn-ghost icon-btn share-close', type: 'button', 'aria-label': 'Close', title: 'Close',
                    onclick: closeShare }, icon('close'))));

    var roleSel = null, expSel = null;
    var input = h('input', { class: 'share-input share-invite-input', type: 'text',
                             placeholder: 'Name or email to invite', maxlength: 254,
                             'aria-label': 'Name or email to invite' });
    var inviteRole = roleSelect('share-invite-role', S.share.inviteRole, function (e) {
      S.share.inviteRole = e.target.value;
    }, 'What an invited person can do');
    var inviteExpiry = shareSelect('share-invite-expiry',
      [['never', 'Never expires'], ['24h', 'Expires in 24 hours'], ['7d', 'Expires in 7 days']],
      S.share.inviteExpiry, function (e) { S.share.inviteExpiry = e.target.value; },
      'When an invited person\u2019s access ends');
    function invite() {
      var name = input.value.trim();
      if (!name) return;
      peoplePost({ action: 'invite', name: name, role: inviteRole.value,
                   expires: inviteExpiry.value === 'never' ? null : inviteExpiry.value });
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); invite(); }
    });
    box.appendChild(h('div', { class: 'share-row share-invite-row' }, input, inviteRole,
      h('button', { class: 'btn btn-primary share-invite-btn', type: 'button', text: S.share.invite ? 'Link made' : 'Invite',
                    onclick: invite })));
    box.appendChild(h('div', { class: 'share-row share-expiry-row' },
      h('span', { class: 'share-label', text: 'Invite expires' }), h('span', { class: 'share-spacer' }), inviteExpiry));
    if (S.share.invite) box.appendChild(shareLinkRow(S.share.invite.url, 'Invite link'));

    box.appendChild(h('div', { class: 'share-sec', text: 'People with access' }));
    (view.people || []).forEach(function (p) {
      var meta = [];
      if (p.email) meta.push(p.email);
      if (p.inherited_role) {
        meta.push('all reviews: ' + p.inherited_role);
        if (p.inherited_expires_at) meta.push('until ' + fmtDate(new Date(p.inherited_expires_at * 1000).toISOString()) + ' (all reviews)');
      }
      if (p.direct_role && p.inherited_role) meta.push('this review: ' + p.direct_role);
      if (p.direct_expires_at) meta.push('until ' + fmtDate(new Date(p.direct_expires_at * 1000).toISOString()) + ' (this review)');
      var personAv = h('span', { class: 'av s', 'aria-hidden': 'true', text: initials(p.name) });
      paintAvatar(personAv, p.user_id ? 'user:' + p.user_id : '', p.name);
      var row = h('div', { class: 'share-row share-person' }, personAv,
        h('span', { class: 'share-person-info' }, h('strong', { text: p.name }),
          meta.length ? h('span', { class: 'share-faint', text: ' ' + meta.join(' \u00b7 ') }) : null));
      if (p.role === 'owner') {
        row.appendChild(h('span', { class: 'share-owner', text: 'Owner' }));
      } else {
        row.appendChild(roleSelect('share-person-role', p.role, function (e) {
          peoplePost({ action: 'grant', user_id: p.user_id, role: e.target.value });
        }, 'Role for ' + p.name));
        if (p.role === 'reviewer' && !p.wake_ok) {
          row.appendChild(h('span', { class: 'share-wake-unconfirmed', text: 'Waking off',
            title: 'This Reviewer is saved, not woken, until you confirm Reviewer again' }));
          row.appendChild(h('button', { class: 'btn btn-ghost share-reconfirm', type: 'button',
            text: 'Confirm Reviewer',
            onclick: function (e) {
              confirmReviewer(e.currentTarget, function () {
                peoplePost({ action: 'grant', user_id: p.user_id, role: 'reviewer' });
              }, function () {});
            } }));
        }
        if (p.direct_role) {
          row.appendChild(h('button', { class: 'btn btn-ghost btn-danger share-remove', type: 'button',
            text: p.inherited_role ? 'Clear override' : 'Remove',
            title: p.inherited_role ? 'Remove this review\u2019s role; their all-reviews access stays'
                                    : 'Remove this person\u2019s role',
            onclick: function () { peoplePost({ action: 'revoke', user_id: p.user_id }); } }));
        } else {
          row.appendChild(h('span', { class: 'share-inherited', text: 'Inherited' }));
        }
      }
      box.appendChild(row);
    });

    box.appendChild(h('div', { class: 'share-sec', text: 'Link' }));
    var seats = h('span', { class: 'share-faint',
      text: 'Live: ' + view.seats.live + ' of ' + view.seats.max + ' seats' });
    if (!link) {
      box.appendChild(h('div', { class: 'share-row' },
        h('span', { class: 'share-faint share-not-shared', text: 'Not shared' }),
        h('span', { class: 'share-spacer' }),
        h('button', { class: 'btn', type: 'button', text: 'Create link',
                      onclick: function () { sharePost({ action: 'create' }); } })));
      box.appendChild(h('div', { class: 'share-row share-seats' }, seats));
    } else {
      box.appendChild(h('div', { class: 'share-row' },
        h('span', { class: 'share-label', text: 'Who can open it' }), h('span', { class: 'share-spacer' }),
        shareSelect('share-audience', [['anyone', 'Anyone with the link'], ['members', 'Signed-in people only']],
          link.audience, function (e) { sharePost({ action: 'update', audience: e.target.value }); },
          'Who can open it')));
      roleSel = roleSelect('share-guest-role', link.guest_role, function (e) {
        sharePost({ action: 'update', guest_role: e.target.value });
      }, 'What guests can do');
      expSel = shareSelect('share-expiry',
        [['never', 'Never expires'], ['24h', 'Expires in 24 hours'], ['7d', 'Expires in 7 days']],
        shareExpiryValue(link), function (e) {
          sharePost({ action: 'update', expires: e.target.value === 'never' ? null : e.target.value });
        }, 'When the link expires');
      box.appendChild(h('div', { class: 'share-row' },
        h('span', { class: 'share-label', text: 'Guests can' }), h('span', { class: 'share-spacer' }), roleSel, expSel));
      if (link.guest_role === 'reviewer' && !link.wake_ok) {
        box.appendChild(h('div', { class: 'share-row share-wake-unconfirmed' },
          h('span', { class: 'share-faint', text: 'Reviewer guests cannot wake Hermes until you confirm Reviewer again.' }),
          h('span', { class: 'share-spacer' }),
          h('button', { class: 'btn share-reconfirm', type: 'button', text: 'Confirm Reviewer',
            onclick: function (e) {
              confirmReviewer(e.currentTarget, function () {
                sharePost({ action: 'update', guest_role: 'reviewer' });
              }, function () {});
            } })));
      }
      box.appendChild(shareLinkRow(link.url, 'Share link'));
      var regen = h('button', { class: 'btn btn-ghost', type: 'button', text: 'Regenerate',
        onclick: function () {
          if (regen.dataset.confirm !== '1') { regen.dataset.confirm = '1'; regen.textContent = 'Regenerate?'; return; }
          regen.disabled = true; sharePost({ action: 'regenerate' });
        } });
      var stop = h('button', { class: 'btn btn-ghost btn-danger', type: 'button', text: 'Stop sharing',
        onclick: function () {
          if (stop.dataset.confirm !== '1') { stop.dataset.confirm = '1'; stop.textContent = 'Stop sharing?'; return; }
          stop.disabled = true; sharePost({ action: 'stop' });
        } });
      box.appendChild(h('div', { class: 'share-row share-seats' }, seats,
        h('span', { class: 'share-spacer' }), regen, stop));
      var roundOptions = [['latest', 'Latest round']].concat((view.rounds || []).map(function (n) {
        return [String(n), 'Round ' + n];
      }));
      box.appendChild(h('div', { class: 'share-row' },
        h('span', { class: 'share-label', text: 'Viewers see' }), h('span', { class: 'share-spacer' }),
        shareSelect('share-round', roundOptions, link.round, function (e) {
          sharePost({ action: 'update', round: e.target.value });
        }, 'What viewer guests see')));
    }
    if (focus) setTimeout(function () { input.focus({ preventScroll: true }); }, 0);
  }

  boot();
})();
