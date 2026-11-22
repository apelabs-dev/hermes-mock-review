/* Threads, comments, presence, typing and activity for the board.
 *
 * One controller per board page. It owns the thread map, the header controls, the thread popover and the two overlays
 * (Comments, Activity), and it keeps them current from `/r/<id>/events`. The board keeps its own pin geometry: this
 * module never writes user text as markup, only through textContent and DOM nodes.
 */
import {resolve, rangeFor, cleanTarget} from './anchors.js';
import {paintAvatar} from './avatar.js';

export function createThreads(opts) {
  'use strict';

  var S = {
    me: null, threads: [], counts: {open: 0, total: 0, unread: 0, activity_unread: 0},
    activity: [], people: [], presence: [], rev: 0, countsRev: 0, loaded: false,
    es: null, failures: 0, chip: null, pop: null, overlay: null, showResolved: false,
    typing: {}, hidden: {}, headerPaints: [], pendingType: {}, created: {}, wantThread: null, menu: null
  };

  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else if (k === 'style') Object.keys(v).forEach(function (s) { if (s.slice(0, 2) === '--') el.style.setProperty(s, v[s]); else el.style[s] = v[s]; });
      else el.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(el, k); }); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function guestMark(cls) {
    return h('span', {class: (cls || 'faint') + ' guest-mark', text: 'guest', title: 'Guest',
      'aria-label': 'Guest'});
  }
  function initials(name) {
    var words = String(name || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/);
    return ((words[0] || '?')[0] + (words.length > 1 ? words[1][0] : (words[0][1] || ''))).toUpperCase();
  }
  var ICON_PATHS = {
    x: '<path d="M18 6 6 18" /> <path d="m6 6 12 12" />',
    send: '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z" /> <path d="m21.854 2.147-10.94 10.939" />',
    'circle-check': '<circle cx="12" cy="12" r="10" /> <path d="m16 9-5.5 5.5L8 12" />',
    'rotate-ccw': '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /> <path d="M3 3v5h5" />',
    more: '<circle cx="12" cy="12" r="1" /> <circle cx="19" cy="12" r="1" /> <circle cx="5" cy="12" r="1" />',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /> <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />',
    pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" /> <path d="m15 5 4 4" />',
    trash: '<path d="M3 6h18" /> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />',
    comments: '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z" />',
    bell: '<path d="M10.268 21a2 2 0 0 0 3.464 0" /> <path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326" />'
  };
  function icon(name) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'ico');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.innerHTML = ICON_PATHS[name] || '';
    return svg;
  }
  function avatar(name, actor, small) {
    var el = h('span', {class: 'av' + (small ? ' s' : ''), 'aria-hidden': 'true', text: initials(name)});
    paintAvatar(el, actor, name);
    return el;
  }
  function ago(at) {
    var sec = Math.max(0, Math.round(Date.now() / 1000 - Number(at || 0)));
    if (sec < 45) return 'now';
    if (sec < 3600) return Math.round(sec / 60) + 'm';
    if (sec < 86400) return Math.round(sec / 3600) + 'h';
    return Math.round(sec / 86400) + 'd';
  }
  function can(cap) { return !!(S.me && (S.me.caps || []).indexOf(cap) >= 0); }
  function safeLink(text) {
    if (typeof text !== 'string' || /[\s\u0000-\u001f\u007f-\u009f]/.test(text) || !/^https?:\/\//i.test(text)) return null;
    try {
      var url = new URL(text);
      return (url.protocol === 'https:' || url.protocol === 'http:') && url.hostname ? url.href : null;
    } catch (e) { return null; }
  }
  function rich(text) {
    var frag = document.createDocumentFragment();
    var re = /(https?:\/\/[^\s<>"']+)|(@[\p{L}\p{N}_.-]{1,40})/gu, last = 0, m;
    function plain(s) { if (s) frag.appendChild(document.createTextNode(s)); }
    while ((m = re.exec(String(text || '')))) {
      plain(String(text).slice(last, m.index));
      if (m[1]) {
        var label = m[1].replace(/[.,!?;:)\]}]+$/, ''), url = safeLink(label);
        if (url) {
          var a = h('a', {text: label});
          a.setAttribute('href', url);
          a.setAttribute('target', '_blank');
          a.setAttribute('rel', 'noopener noreferrer');
          frag.appendChild(a);
        } else plain(label);
        plain(m[1].slice(label.length));
      } else frag.appendChild(h('span', {class: 'men', text: m[2]}));
      last = m.index + m[0].length;
    }
    plain(String(text || '').slice(last));
    return frag;
  }

  function redraw() { try { opts.redraw(); } catch (e) {} }
  function request(path, body) {
    var init = {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store'};
    if (body !== undefined) {
      init.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': opts.csrf()};
      init.body = JSON.stringify(body);
    }
    return fetch('api/' + path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (!res.ok) {
          var e = new Error(j.error || ('HTTP ' + res.status));
          e.status = res.status;
          throw e;
        }
        return j;
      });
    });
  }

  function find(id) {
    for (var i = 0; i < S.threads.length; i++) if (S.threads[i].id === id) return S.threads[i];
    return null;
  }
  function openThreads() { return S.threads.filter(function (t) { return t.status !== 'resolved' && !S.hidden[t.id]; }); }
  function mirrorSet() { return opts.mirrored ? opts.mirrored() : {}; }
  function unmirrored(t) { return !mirrorSet()[t.id]; }
  function anchor(t) { return (t && t.anchor) || {}; }
  function firstText(t) {
    var list = (t.comments || []).filter(function (c) { return c.deleted_at === null; });
    return list.length ? list[0].text : '(deleted)';
  }

  // ---- Hermes pin activity -------------------------------------------
  var deadlineTimer = 0, elapsedTimer = 0;
  function roundRequests() {
    return Object.values(opts.pinRequests()).filter(function (q) {
      return !q.thread_id || !S.hidden[q.thread_id];
    });
  }
  function working(q) { return !!q && (q.awaiting_delivery || q.deadline * 1000 > opts.pinNow()); }
  function activityFor(thread, pin) {
    var matches = roundRequests().filter(function (q) {
      return q.thread_id ? q.thread_id === thread : !!pin && (q.pin_id || q.id) === pin.id &&
        q.block === pin.block && (q.option || null) === (pin.option || null);
    });
    return matches.find(working) || matches[matches.length - 1];
  }
  function activityState(q) {
    if (!q) return '';
    if (working(q)) return 'working';
    return q.status === 'failed' || q.deadline ? 'failed' : '';
  }
  function activityTextFor(q) {
    return activityState(q) === 'working' ? 'Hermes is working on this…'
      : q.timed_out || q.deadline ? 'Hermes couldn’t finish in time.' : 'Hermes couldn’t finish.';
  }
  function paintPin(btn, q) {
    var state = activityState(q);
    if ((btn.dataset.hermesState || '') === state) return btn;
    var old = btn.querySelector('.hermes-pin-indicator');
    if (old) old.remove();
    if (state) {
      btn.dataset.hermesState = state;
      btn.appendChild(h('span', {class: 'hermes-pin-indicator', 'aria-hidden': 'true'},
        state === 'failed' ? icon('x') : null));
    } else delete btn.dataset.hermesState;
    var label = btn.dataset.pinLabel || btn.getAttribute('aria-label');
    btn.dataset.pinLabel = label;
    btn.setAttribute('aria-label', state ? label + ' · ' + activityTextFor(q) : label);
    return btn;
  }
  function statusRow(row, q, retry) {
    var state = activityState(q);
    var current = row.querySelector('.hermes-thread-status');
    var key = state ? [q.id, state, retry, activityTextFor(q)].join('/') : '';
    if (current && current.dataset.statusKey === key) return;
    clearInterval(elapsedTimer);
    if (current) current.remove();
    if (!state) return;
    var elapsed = state === 'working' ? h('time', {class: 'hermes-elapsed', 'aria-hidden': 'true'}) : null;
    row.prepend(h('div', {class: 'hermes-thread-status', 'data-hermes-state': state, 'data-status-key': key},
      h('span', {role: 'status', text: activityTextFor(q)}), elapsed,
      state === 'failed' && retry ? h('span', {class: 'hermes-retry', text: 'Mention @Hermes again to retry.'}) : null));
    if (!elapsed) return;
    function tick() {
      var seconds = Math.max(0, Math.floor((opts.pinNow() - Date.parse(q.requested_at)) / 1000));
      elapsed.textContent = Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
    }
    tick();
    elapsedTimer = setInterval(tick, 1000);
  }
  function pinActivityChanged() {
    clearTimeout(deadlineTimer);
    var now = opts.pinNow();
    var deadlines = roundRequests().map(function (q) { return q.deadline * 1000; })
      .filter(function (deadline) { return deadline > now; });
    if (deadlines.length) deadlineTimer = setTimeout(pinActivityChanged, Math.min.apply(null, deadlines) - now);
    syncHeader();
    repaintTyping();
    if (S.overlay && S.overlay.kind === 'comments') S.overlay.paint();
    opts.paintPinActivity();
  }

  // ---- server state ---------------------------------------------------
  function refresh() {
    var view = opts.view(), before = S.rev;
    return request('threads?round=' + encodeURIComponent(view)).then(function (j) {
      if (view !== opts.view()) return;
      var fresh = j.threads || [];
      if ((j.rev || 0) < S.rev) {
        var have = {};
        fresh.forEach(function (t) { have[t.id] = true; });
        S.threads.forEach(function (t) { if (!have[t.id]) fresh.push(t); });
      } else {
        S.threads.forEach(function (t) { if (S.created[t.id]) {
          var seen = fresh.some(function (x) { return x.id === t.id; });
          if (!seen) fresh.push(t);
        } });
      }
      S.threads = fresh;
      S.counts = Object.assign({open: 0, total: 0, unread: 0, activity_unread: 0}, j.counts || {});
      // The revision whose effects are already baked into S.counts; onEvent uses it to drop replayed frames.
      S.countsRev = j.rev || 0;
      S.activity = j.activity || [];
      S.people = j.people || [];
      S.rev = Math.max(before, j.rev || 0);
      S.loaded = true;
      syncHeader();
      if (S.overlay) S.overlay.paint();
      if (S.pop && S.pop.render) S.pop.render();
      redraw();
      maybeOpenWanted();
    }).catch(function () {});
  }

  function boot(me) {
    S.me = me || null;
    S.presence = [];
    var wanted = '';
    try { wanted = new URLSearchParams(location.search).get('thread') || ''; } catch (e) {}
    S.wantThread = /^[a-f0-9]{32}$/.test(wanted) ? wanted : null;
    syncHeader();
    refresh();
    connect();
  }
  function maybeOpenWanted() {
    if (!S.wantThread || !S.loaded) return;
    var wanted = S.wantThread;
    S.wantThread = null;
    var t = find(wanted);
    if (t) open(t);
  }

  function setView() {
    pinActivityChanged();
    refresh();
  }

  function connect() {
    if (S.es || typeof EventSource === 'undefined') return;
    try { S.es = new EventSource('events'); } catch (e) { S.es = null; return; }
    var opened = false;
    S.es.onopen = function () {
      S.failures = 0; chip(false);
      if (opened) opts.refreshPinActivity();
      opened = true;
    };
    S.es.onerror = function () {
      S.failures++;
      if (S.failures >= 2) chip(true);
    };
    S.es.addEventListener('hello', function (e) { onHello(e); });
    S.es.addEventListener('presence', function (e) { onPresence(e); });
    S.es.addEventListener('typing', function (e) { onTyping(e); });
    S.es.addEventListener('pin.requests', function () { opts.refreshPinActivity(); });
    S.es.addEventListener('reset', function () { if (opts.reloadState) opts.reloadState(); else refresh(); });
    ['thread.created', 'thread.replied', 'thread.status', 'thread.deleted', 'comment.edited', 'comment.deleted',
      'mention', 'review.reset'].forEach(function (kind) {
      S.es.addEventListener(kind, function (e) { onEvent(kind, e); });
    });
  }
  function chip(on) {
    if (!on) { if (S.chip) { S.chip.remove(); S.chip = null; } return; }
    if (S.chip) return;
    S.chip = h('div', {class: 'live-chip', role: 'status', text: 'Live: reconnecting'});
    document.body.appendChild(S.chip);
  }
  function parse(e) {
    try { return JSON.parse(e.data || '{}'); } catch (err) { return {}; }
  }
  function onHello(e) {
    var d = parse(e);
    if (S.me && d.actor) S.me.actor = d.actor;
    if (d.presence) { S.presence = d.presence.people || []; syncHeader(); }
  }
  function onPresence(e) {
    var d = parse(e);
    S.presence = d.people || [];
    syncHeader();
    repaintTyping();
  }
  function onTyping(e) {
    var d = parse(e);
    if (!d.thread || (S.me && d.actor === S.me.actor)) return;
    S.typing[d.thread] = {name: d.name || 'Someone', until: Date.now() + 5000};
    if (S.pop && S.pop.id === d.thread) repaintTyping();
    setTimeout(function () { if (S.typing[d.thread] && S.typing[d.thread].until <= Date.now()) { delete S.typing[d.thread]; repaintTyping(); redraw(); } }, 5100);
    redraw();
  }
  function repaintTyping() {
    // renderTyping lives inside open(); the SSE handlers are at module scope, so they go through the popover.
    if (S.pop && S.pop.typing) S.pop.typing();
  }
  function onEvent(kind, e) {
    var d = parse(e), t = d.thread;
    var mine = S.me && d.actor === S.me.actor;
    var id = Number(e.lastEventId) || 0;
    // A fresh EventSource starts at cursor 0, so the server replays the review's whole history. Frames at or below
    // countsRev are already reflected in the fetched snapshot: keep their thread state and repaints, but never add
    // their counters twice. Only frames above the watermark are live. A `reset` refreshes the snapshot and with it
    // the watermark, and a reconnect resumes from the EventSource's own cursor, so neither resurrects a count.
    var stale = id > 0 && id <= S.countsRev;
    if (e.lastEventId) S.rev = Math.max(S.rev, id);
    if (kind === 'thread.deleted') {
      var gone = d.thread_id || (t && t.id);
      if (gone) {
        S.hidden[gone] = true;
        S.threads = S.threads.filter(function (x) { return x.id !== gone; });
        if (S.pop && S.pop.id === gone) closePopover();
      }
    } else if (t) {
      upsert(t);
      var stored = find(t.id);
      if (!stale && !mine && kind !== 'comment.deleted') {
        if (stored) stored.unread = true;
        S.counts.unread = (S.counts.unread || 0) + 1;
      }
      if (!stale && !mine && kind === 'thread.created') S.counts.open = (S.counts.open || 0) + 1;
      if (!stale && kind === 'thread.status' && t.status === 'resolved') S.counts.open = Math.max(0, (S.counts.open || 0) - 1);
      if (!stale && kind === 'thread.status' && t.status === 'open') S.counts.open = (S.counts.open || 0) + 1;
    }
    if (!stale && ['thread.created', 'thread.replied', 'thread.status', 'mention'].indexOf(kind) >= 0) {
      // The fetched snapshot lists the reader's own events too (only `unread` is theirs to lose), so a live own
      // event must enter the list as well: a feed opened before the snapshot arrives must not miss the reader's
      // own row, e.g. a guest's thread.created, which is the only row that carries the guest mark.
      if (!mine) S.counts.activity_unread = (S.counts.activity_unread || 0) + 1;
      S.activity.unshift({id: id, kind: kind, thread_id: (t && t.id) || d.thread_id || '',
        actor: d.actor, at: Date.now() / 1000, data: d, unread: !mine});
      if (S.activity.length > 100) S.activity.length = 100;
    }
    if (S.overlay && S.overlay.kind === 'activity') S.overlay.paint();
    if (S.overlay && S.overlay.kind === 'comments') S.overlay.paint();
    syncHeader();
    redraw();
  }
  function upsert(t) {
    (t.comments || []).forEach(function (c) { c.mine = !!(S.me && S.me.actor && c.author === S.me.actor); });
    delete t.unread;   // unread is per reader: the local one survives the merge
    var i = -1;
    for (var j = 0; j < S.threads.length; j++) if (S.threads[j].id === t.id) { i = j; break; }
    if (i < 0) S.threads.push(t);
    // Every mutation bumps updated_at, so it is a per-thread revision: a replayed frame, a late reply frame or a
    // slower response must not downgrade a thread that already moved on. An equal or unknown revision still merges.
    else if (!(Number(S.threads[i].updated_at) > Number(t.updated_at))) S.threads[i] = Object.assign({}, S.threads[i], t);
    S.threads.sort(function (a, b) { return (b.activated - a.activated) || (b.updated_at - a.updated_at); });
    if (S.pop && S.pop.id === t.id && S.pop.render) S.pop.render();
  }

  // ---- header ---------------------------------------------------------
  function countBadge(n) { return h('span', {class: 'badge', text: String(n || 0)}); }
  function workingCount() {
    var button = h('button', {class: 'btn hermes-working-count', type: 'button', 'aria-haspopup': 'dialog',
      onclick: function () { openComments(true); }});
    var slot = h('span', {class: 'hermes-count-slot'});
    function paint() {
      var count = roundRequests().filter(working).length;
      if (!count) { button.remove(); return; }
      button.textContent = 'Hermes · ' + count + ' working';
      if (!button.isConnected) slot.appendChild(button);
    }
    paint();
    S.headerPaints.push({nodes: [slot], paint: paint});
    return slot;
  }
  function syncHeader() {
    S.headerPaints = S.headerPaints.filter(function (p) { return p.nodes.some(function (n) { return n.isConnected; }); });
    S.headerPaints.forEach(function (p) { p.paint(); });
  }
  function headerActions() {
    var avatars = h('span', {class: 'tavs', 'aria-hidden': 'true'});
    var here = h('span', {class: 'there'});
    var commentsBadge = countBadge(0), activityBadge = countBadge(0);
    var commentsBtn = h('button', {class: 'tbtn tcomments', type: 'button', title: 'All comments on this review',
      onclick: function () { openComments(false); }}, icon('comments'), h('span', {text: 'Comments'}), commentsBadge);
    var activityBtn = h('button', {class: 'tbtn tactivity', type: 'button', title: 'Recent activity',
      onclick: openActivity}, icon('bell'), h('span', {text: 'Activity'}), activityBadge);
    var nodes = [avatars, here, commentsBtn, activityBtn];
    function paint() {
      avatars.textContent = '';
      S.presence.slice(0, 5).forEach(function (p) { avatars.appendChild(avatar(p.name, p.actor, true)); });
      here.textContent = S.presence.length ? S.presence.length + ' here' : '';
      here.hidden = !S.presence.length;
      commentsBadge.textContent = String(S.counts.open || 0);
      commentsBtn.classList.toggle('has-unread', (S.counts.unread || 0) > 0);
      activityBadge.textContent = String(S.counts.activity_unread || 0);
      activityBtn.classList.toggle('has-unread', (S.counts.activity_unread || 0) > 0);
    }
    paint();
    S.headerPaints.push({nodes: nodes, paint: paint});
    return h('span', {class: 'tacts'}, nodes);     // one flex group, so the narrow-screen rules can drop "N here"
  }

  // ---- overlays -------------------------------------------------------
  function overlayFocusables(el) {
    return Array.prototype.filter.call(el.querySelectorAll('button,textarea,input,select,a[href],[tabindex]:not([tabindex="-1"])'),
      function (n) { return !n.disabled && n.offsetParent !== null; });
  }
  function closeOverlay() {
    if (!S.overlay) return;
    var o = S.overlay;
    S.overlay = null;
    o.el.remove();
    document.body.classList.remove('overlay-open');
    if (o.onClose) o.onClose();
    if (o.returnTo && o.returnTo.isConnected) o.returnTo.focus({preventScroll: true});
  }
  function overlay(kind, title) {
    closeOverlay();
    var returnTo = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    var body = h('div', {class: 'tbody'});
    var panel = h('div', {class: 'tpanel', tabindex: '-1'}, h('div', {class: 'thead'},
      h('strong', {text: title}), h('span', {class: 'spacer'}),
      h('button', {class: 'btn btn-ghost tclose', type: 'button', 'aria-label': 'Close', text: 'Close',
        onclick: closeOverlay})), body);
    var el = h('div', {class: 'toverlay', role: 'dialog', 'aria-modal': 'true', 'aria-label': title}, panel);
    el.addEventListener('click', function (e) { if (e.target === el) closeOverlay(); });
    document.body.appendChild(el);
    document.body.classList.add('overlay-open');
    var o = {kind: kind, el: el, body: body, returnTo: returnTo, paint: null};
    S.overlay = o;
    requestAnimationFrame(function () {
      if (S.overlay !== o) return;
      var f = overlayFocusables(el);
      (f[0] || panel).focus({preventScroll: true});
    });
    return o;
  }
  function threadRow(t, here) {
    var dot = t.unread ? h('span', {class: 'un', title: 'Unread'}) : null;
    var meta = h('span', {class: 'tmeta'},
      h('span', {text: (t.comments || []).length + ' comment' + ((t.comments || []).length === 1 ? '' : 's')}),
      h('span', {text: '· ' + ago(t.updated_at)}));
    var badge = working(activityFor(t.id)) ? h('span', {class: 'badge ac', text: 'Working'}) : null;
    return h('button', {class: 'trow' + (t.activated ? ' act' : ''), type: 'button', onclick: function () {
      closeOverlay();
      if (here) open(t); else jump(t);
    }}, h('span', {class: 'tnum', text: String(t.n)}), h('span', {class: 'tmain'},
      h('span', {class: 'tname'}, dot, h('span', {text: firstText(t)})),
      h('span', {class: 'tsub', text: anchorLabel(t)})), badge, meta);
  }
  function anchorLabel(t) {
    var a = anchor(t), block = a.block || 'board';
    var where = a.option ? block.toUpperCase() + ' · ' + String(a.option).toUpperCase() : block === 'proto' ? 'Prototype'
      : a.on === 'board' ? 'Board' : block;
    return t.status === 'resolved' ? where + ' · Resolved' : where;
  }
  function openComments(onlyWorking) {
    closePopover();
    var o = overlay('comments', onlyWorking ? 'Working comments' : 'Comments');
    var toggle = h('input', {type: 'checkbox'});
    toggle.checked = S.showResolved;
    toggle.addEventListener('change', function () { S.showResolved = toggle.checked; o.paint(); });
    if (!onlyWorking) o.body.appendChild(h('div', {class: 'tfilter'},
      h('label', {class: 'ttoggle'}, toggle, h('span', {class: 'tr'}), h('span', {text: 'Resolved'}))));
    var list = h('div', {class: 'tlist'});
    o.body.appendChild(list);
    o.paint = function () {
      var rows = S.threads.slice().sort(function (a, b) {
        return (b.activated - a.activated) || (b.updated_at - a.updated_at);
      }).filter(function (t) { return onlyWorking ? working(activityFor(t.id)) : S.showResolved || t.status !== 'resolved'; });
      var fixes = roundRequests().filter(function (q) {
        return !q.thread_id && (onlyWorking ? working(q) : !!activityState(q) || q.status === 'done');
      });
      list.textContent = '';
      if (!rows.length && !fixes.length) {
        list.appendChild(h('p', {class: 'tempty', text: onlyWorking ? 'Hermes has no work on this round.' : S.showResolved
          ? 'No comments yet — press the comment button, then click anywhere on the board to add one.'
          : 'No open comments. Turn on "Resolved" to see the rest.'}));
        return;
      }
      rows.forEach(function (t) { list.appendChild(threadRow(t, onlyWorking || !!activityState(activityFor(t.id)))); });
      fixes.forEach(function (q) {
        var state = activityState(q);
        list.appendChild(h('button', {class: 'trow', type: 'button', onclick: function () {
          closeOverlay();
          opts.openPinFix(q);
        }}, h('span', {class: 'tmain'}, h('span', {class: 'tname', text: 'Pin fix'}),
          h('span', {class: 'tsub', text: opts.blockLabel(q.block)})),
          state === 'working' ? h('span', {class: 'badge ac', text: 'Working'}) : null));
      });
    };
    o.paint();
  }
  function activityText(e) {
    var name = (e.data && e.data.name) || 'Someone', t = e.data && e.data.thread;
    var n = t && t.n ? ' ' + t.n : '';
    if (e.kind === 'thread.created') return name + ' started thread' + n;
    if (e.kind === 'thread.replied') return name + ' replied to thread' + n;
    if (e.kind === 'thread.status') return name + ((t && t.status === 'resolved') ? ' resolved thread' : ' reopened thread') + n;
    if (e.kind === 'mention') {
      var targets = (e.data && e.data.targets) || [];
      return name + (S.me && targets.indexOf(S.me.actor) >= 0 ? ' mentioned you' : ' mentioned someone');
    }
    if (e.kind === 'comment.edited') return name + ' edited a comment';
    if (e.kind === 'comment.deleted') return name + ' deleted a comment';
    if (e.kind === 'thread.deleted') return name + ' deleted a thread';
    return name + ' updated a thread';
  }
  function openActivity() {
    var o = overlay('activity', 'Activity');
    var list = h('div', {class: 'tlist feed'});
    o.body.appendChild(list);
    o.paint = function () {
      list.textContent = '';
      if (!S.activity.length) {
        list.appendChild(h('p', {class: 'tempty', text: 'Nothing yet. Replies, resolutions and mentions show up here.'}));
        return;
      }
      S.activity.forEach(function (e) {
        var t = e.data && e.data.thread;
        var kind = e.actor_kind || (e.data && e.data.actor_kind);
        var row = h('button', {class: 'trow' + (e.unread ? ' unrow' : ''), type: 'button', onclick: function () {
          closeOverlay();
          var th = find(e.thread_id);
          if (th) jump(th);
        }}, h('span', {class: 'tadot'}), h('span', {class: 'tmain'},
          h('span', {class: 'tname'}, kind === 'guest' ? guestMark('tsub') : null,
            h('span', {text: activityText(e)})),
          h('span', {class: 'tsub', text: t ? anchorLabel(t) : ''})),
          h('span', {class: 'tmeta', text: ago(e.at)}));
        list.appendChild(row);
      });
    };
    o.paint();
    request('activity/read', {}).then(function () {
      S.counts.activity_unread = 0;
      S.activity.forEach(function (e) { e.unread = false; });
      syncHeader();
      if (S.overlay === o) o.paint();
    }, function () {});
  }

  function jump(t) {
    var view = opts.view();
    if (t.round && t.round !== view) {
      opts.setView(t.round);
      setTimeout(function () { open(t); }, 420);
    } else {
      open(t);
    }
  }

  // ---- typing and mentions -------------------------------------------
  function typing(thread) {
    var now = Date.now(), key = thread;
    if (S.pendingType[key] && now - S.pendingType[key] < 2000) return;
    S.pendingType[key] = now;
    request('typing', {thread: thread}).catch(function () {});
  }
  function attachMentions(ta) {
    ta._mrMentions = [];
    var list = null, items = [], active = 0, tokenStart = -1, tokenEnd = -1;
    function candidates(q) {
      var out = [];
      var low = String(q || '').toLowerCase();
      S.people.forEach(function (p) {
        if (!low || String(p.name || '').toLowerCase().indexOf(low) >= 0 || String(p.target).toLowerCase().indexOf(low) >= 0) out.push(p);
      });
      if (hermesAllowed() && (low === '' || 'hermes'.indexOf(low) >= 0)) out.push({kind: 'hermes', target: 'hermes', id: 'hermes', name: 'Hermes', role: 'agent'});
      return out.slice(0, 8);
    }
    function closeList() {
      if (list) { list.remove(); list = null; }
      items = [];
      tokenStart = tokenEnd = -1;
    }
    function place() {
      if (!list) return;
      var r = ta.getBoundingClientRect();
      list.style.left = Math.max(8, r.left) + 'px';
      list.style.top = Math.min(window.innerHeight - 44, r.bottom + 4) + 'px';
    }
    function paint() {
      if (!list) return;
      list.textContent = '';
      items.forEach(function (c, i) {
        list.appendChild(h('div', {class: i === active ? 'sel' : '', 'data-i': String(i), onpointerdown: function (e) {
          e.preventDefault();
          accept(i);
        }}, avatar(c.name, c.target, true), h('span', {class: 'ml-name', text: c.name}),
          h('span', {class: 'faint', text: c.role || ''})));
      });
      place();
    }
    function accept(i) {
      var c = items[i];
      if (!c || tokenStart < 0) return;
      var value = ta.value, before = value.slice(0, tokenStart), after = value.slice(tokenEnd);
      var insert = '@' + c.name + ' ';
      ta.value = before + insert + after;
      var caret = (before + insert).length;
      try { ta.setSelectionRange(caret, caret); } catch (e) {}
      ta._mrMentions.push({target: c.target, label: c.name});
      closeList();
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.focus();
    }
    function update() {
      var caret = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
      var before = ta.value.slice(0, caret);
      var m = /(^|\s)@([\p{L}\p{N}_.-]{0,40})$/u.exec(before);
      if (!m) { closeList(); return; }
      tokenStart = caret - m[2].length - 1;
      tokenEnd = caret;
      var found = candidates(m[2]);
      if (!found.length) { closeList(); return; }
      if (!list) {
        list = h('div', {class: 'ml', role: 'listbox', 'aria-label': 'Mention'});
        document.body.appendChild(list);
      }
      items = found;
      active = Math.min(active, items.length - 1);
      paint();
    }
    ta.addEventListener('input', update);
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeList(); return; }
      if (!list) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); active = (active + 1) % items.length; paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = (active - 1 + items.length) % items.length; paint(); }
      else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); accept(active); }
    });
    ta.addEventListener('blur', function () { setTimeout(closeList, 120); });
    return ta;
  }
  function mentionsFor(ta) {
    if (!ta || !ta._mrMentions) return [];
    var text = ta.value || '';
    return ta._mrMentions.filter(function (m) { return text.indexOf('@' + m.label) >= 0; }).map(function (m) {
      return m.target;
    });
  }

  // ---- the composer's Send to Hermes box, shared by a new pin and a reply ----
  // The owner and any reviewer (a share-link guest or a member) get the box; commenters and viewers never. The board
  // server decides whether the mention wakes Hermes or is saved (wakeguard.request_gate), and says why in `wake.detail`.
  function hermesAllowed() { return can('submit') && (!opts.canSendToHermes || opts.canSendToHermes()); }
  function wakeNote(j) {
    var w = j && j.wake;
    if (w && w.status === 'stored' && w.detail) opts.toast(w.detail, 'warn');
  }
  function hasHermesToken(v) { return /(^|\s)@Hermes(?=\s|$)/.test(String(v || '')); }
  function setHermesMention(ta, on) {
    if (!ta._mrMentions) ta._mrMentions = [];
    ta._mrMentions = ta._mrMentions.filter(function (m) { return m.target !== 'hermes'; });
    if (on) ta._mrMentions.push({target: 'hermes', label: 'Hermes'});
  }
  function addLeadingHermes(ta) {
    if (/^@Hermes\s/.test(ta.value)) { setHermesMention(ta, true); return; }
    var at = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
    ta.value = ('@Hermes ' + ta.value).slice(0, ta.maxLength > 0 ? ta.maxLength : 2000);
    var caret = Math.min(ta.value.length, Math.max(8, at + 8));
    try { ta.setSelectionRange(caret, caret); } catch (e) {}
    setHermesMention(ta, true);
  }
  function dropLeadingHermes(ta) {
    if (!/^@Hermes\s/.test(ta.value)) return;
    var at = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
    ta.value = ta.value.slice(8);
    var caret = Math.max(0, Math.min(ta.value.length, at - 8));
    try { ta.setSelectionRange(caret, caret); } catch (e) {}
  }
  function meaningfulText(ta) {   // the default leading token alone is not a comment
    var text = ta ? ta.value.trim() : '';
    return text === '@Hermes' ? '' : text;
  }
  function composerBar(ta, o) {
    o = o || {};
    var allowed = hermesAllowed(), box = null, label = null;
    if (allowed) {
      box = h('input', {class: 'hermes-box', type: 'checkbox'});
      label = h('label', {class: 'hermes-send', title: 'Post this with @Hermes so the agent acts on it'},
        box, h('span', {text: 'Send to Hermes'}));
    }
    function sync() {
      if (!box) return;
      var on = hasHermesToken(ta.value);
      box.checked = on;
      setHermesMention(ta, on);
    }
    if (box) {
      if (o.keep) sync();
      else { box.checked = true; addLeadingHermes(ta); }
      box.addEventListener('change', function () {
        if (box.checked) addLeadingHermes(ta); else { dropLeadingHermes(ta); setHermesMention(ta, false); }
        ta.focus();
      });
      ta.addEventListener('input', sync);
    }
    var row = h('div', {class: o.cls || 'tp-actions'}, label || null, h('span', {class: 'spacer'}), o.send || null);
    return {el: row, box: box, allowed: allowed, sync: sync};
  }

  // ---- the thread popover --------------------------------------------
  function closePopMenu() {
    if (!S.menu) return;
    var m = S.menu;
    S.menu = null;
    m.el.remove();
    if (m.btn && m.btn.isConnected) m.btn.setAttribute('aria-expanded', 'false');
  }
  function showPopMenu(btn, label, items) {
    closePopMenu();
    var menu = h('div', {class: 'menu menu-pop tp-menu', role: 'menu', 'aria-label': label},
      items.filter(Boolean).map(function (it) {
        return h('button', {class: 'menu-item' + (it.danger ? ' menu-danger' : ''), type: 'button', role: 'menuitem',
          onclick: function () { closePopMenu(); it.run(); }}, icon(it.icon), h('span', {text: it.text}));
      }));
    document.body.appendChild(menu);
    var r = btn.getBoundingClientRect();
    var left = Math.min(Math.max(8, r.right - menu.offsetWidth), Math.max(8, window.innerWidth - menu.offsetWidth - 8));
    var top = r.bottom + 4;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - menu.offsetHeight - 4);
    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(top) + 'px';
    btn.setAttribute('aria-expanded', 'true');
    S.menu = {el: menu, btn: btn};
    requestAnimationFrame(function () {
      var first = menu.querySelector('.menu-item');
      if (first) first.focus({preventScroll: true});
    });
  }
  function copyThreadLink(t) {
    var url;
    try { url = new URL(location.href); } catch (e) { url = null; }
    if (!url) return;
    url.searchParams.set('thread', t.id);
    var text = url.href;
    function done() { opts.toast('Link copied'); }
    function manual() {
      var field = h('textarea', {class: 'sr-copy', 'aria-hidden': 'true'});
      field.value = text;
      document.body.appendChild(field);
      field.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      field.remove();
      if (ok) done(); else opts.toast('Copy this link: ' + text, 'warn');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, manual);
    } else manual();
  }
  function closePopover() {
    if (!S.pop) return;
    closePopMenu();
    var p = S.pop;
    S.pop = null;
    clearInterval(elapsedTimer);
    p.el.remove();
    var returnTo = p.returnTo && p.returnTo.isConnected ? p.returnTo : markerFor(p.id);
    if (returnTo) returnTo.focus({ preventScroll: true });
    if (p.onClose) p.onClose();
  }
  function markerFor(id) {
    var all = document.querySelectorAll('[data-thread]');
    for (var i = 0; i < all.length; i++) if (all[i].getAttribute('data-thread') === id) return all[i];
    return null;
  }
  function whereLabel(t) {
    var a = anchor(t), target = a.target ? cleanTarget(a.target) : null;
    if (target && target.label) return 'on ' + target.label;
    return a.on === 'board' ? 'on the board' : anchorLabel(t);
  }
  function placePopover(el, box) {
    var sheet = window.innerWidth < 640;
    el.classList.toggle('sheet', sheet);
    if (sheet) { el.setAttribute('aria-modal', 'true'); return; }
    var w = el.offsetWidth, hh = el.offsetHeight;
    var ax = box ? box.right : window.innerWidth / 2, ay = box ? box.top : window.innerHeight / 2;
    var left = ax + 18;
    if (left + w > window.innerWidth - 12) left = ax - w - 18;
    el.style.left = Math.max(12, left) + 'px';
    el.style.top = Math.max(12, Math.min(ay - 28, window.innerHeight - hh - 12)) + 'px';
  }
  function openFix(q, marker, box) {
    closePopover();
    var close = h('button', {class: 'btn btn-ghost tp-act tp-close', type: 'button',
      title: 'Close', 'aria-label': 'Close', onclick: closePopover}, icon('x'));
    var row = h('div', {class: 'tp-typing'});
    var el = h('div', {class: 'popover tpop', role: 'dialog', 'aria-label': 'Pin fix'},
      h('div', {class: 'tp-head'}, h('strong', {text: 'Pin fix'}), h('span', {class: 'spacer'}), close),
      h('div', {class: 'tp-anchor', text: opts.blockLabel(q.block)}), row);
    var p = S.pop = {el: el, returnTo: marker || document.activeElement, typing: function () {
      var request = opts.pinRequests()[q.id];
      statusRow(row, request, false);
      if (!activityState(request) && request && request.status === 'done' && !row.firstChild)
        row.appendChild(h('span', {class: 'tp-note', text: 'Pin fix completed.'}));
    }};
    document.body.appendChild(el);
    p.typing();
    placePopover(el, box || (marker && marker.getBoundingClientRect()));
    close.focus({preventScroll: true});
  }
  function open(thread, extra) {
    var t = typeof thread === 'string' ? find(thread) : thread;
    if (!t) return;
    closePopover();
    extra = extra || {};
    var ta = null;
    var typingRow = h('div', {class: 'tp-typing'});
    var replyWrap = null;
    var replyBar = null;
    var el = h('div', {class: 'popover tpop', role: 'dialog', 'aria-label': 'Thread ' + t.n});
    var p = {id: t.id, el: el, thread: t, extra: extra, edit: null};
    S.pop = p;
    document.body.appendChild(el);
    renderPopBody();
    // A frame may open the thread under the marker it drew; that message is untrusted HTML's word, so it never
    // changes the read state. Read is a trusted tap on the board, which is marked before it gets here.
    if (!extra || extra.read !== false) markRead(t);
    var marker = markerFor(t.id);
    p.returnTo = marker;
    placePopover(el, marker ? marker.getBoundingClientRect() : extra.box);
    function renderPopBody() {
      closePopMenu();
      var prev = ta, keep = prev ? {
        value: prev.value,
        selectionStart: prev.selectionStart,
        selectionEnd: prev.selectionEnd,
        focused: document.activeElement === prev,
        mentions: (prev._mrMentions || []).slice()
      } : null;
      var fresh = !!(p.freshReply && (!keep || !keep.value));   // a just-sent reply is not a kept draft
      if (p.freshReply) p.freshReply = false;
      var editor = p.edit ? p.el.querySelector('.tp-edit') : null;   // an open comment edit survives every repaint
      if (editor) {
        p.edit.value = editor.value;
        p.edit.start = editor.selectionStart;
        p.edit.end = editor.selectionEnd;
        p.edit.focused = document.activeElement === editor;
      }
      ta = null;
      replyBar = null;
      el.textContent = '';
      var t = p.thread = find(p.id) || p.thread;
      var resolved = t.status === 'resolved';
      var first = (t.comments || []).filter(function (c) { return c.deleted_at === null; })[0] || (t.comments || [])[0] || {};
      var who = first.author_name || 'Someone';
      var root = (t.comments || [])[0] || null;
      var ownRoot = !!(root && root.mine && root.deleted_at === null);
      var actions = h('span', {class: 'tp-acts'},
        can('comment') ? h('button', {class: 'btn btn-ghost tp-act tp-resolve' + (resolved ? ' on' : ''), type: 'button',
          'aria-label': resolved ? 'Reopen' : 'Resolve', title: resolved ? 'Reopen' : 'Resolve',
          'aria-pressed': resolved ? 'true' : 'false',
          onclick: function () { setStatus(t, resolved ? 'open' : 'resolved'); }}, icon(resolved ? 'rotate-ccw' : 'circle-check')) : null,
        h('button', {class: 'btn btn-ghost tp-act tp-more', type: 'button', 'aria-label': 'More', title: 'More',
          'aria-haspopup': 'menu', 'aria-expanded': 'false',
          onclick: function (e) {
            e.preventDefault(); e.stopPropagation();
            var items = [];
            if (ownRoot) items.push({icon: 'pencil', text: 'Edit', run: function () { editComment(p, root); }});
            items.push({icon: 'link', text: 'Copy link', run: function () { copyThreadLink(t); }});
            if (ownRoot) items.push({icon: 'trash', text: 'Delete', danger: true,
              run: function () { deleteComment(p, root); }});
            showPopMenu(this, 'Thread actions', items);
          }}, icon('more')),
        h('button', {class: 'btn btn-ghost tp-act tp-close', type: 'button', 'aria-label': 'Close', title: 'Close',
          onclick: closePopover}, icon('x')));
      var head = h('div', {class: 'tp-head'}, avatar(who, first.author, true),
        h('div', {class: 'tp-title'}, h('strong', {text: who}),
          first.author_kind === 'guest' ? [' ', guestMark()] : null,
          h('span', {class: 'faint', text: ' · ' + ago(first.created_at)})),
        h('span', {class: 'spacer'}), actions);
      el.appendChild(head);
      el.appendChild(h('div', {class: 'tp-anchor'},
        h('span', {class: 'pin-static', text: String(t.n)}), h('span', {class: 'tp-where', text: whereLabel(t)})));
      if (extra.detached) {
        el.appendChild(h('div', {class: 'detached-state'}, h('span', {class: 'st warn', text: 'Element removed'}),
          h('div', {class: 'faint', text: 'This thread stays here and re-attaches if the element comes back.'})));
      }
      var list = h('div', {class: 'tp-comments'});
      (t.comments || []).forEach(function (c) { list.appendChild(commentRow(p, c)); });
      el.appendChild(list);
      el.appendChild(typingRow);
      replyWrap = h('div', {class: 'tp-reply'});
      if (t.status === 'resolved') {
        replyWrap.appendChild(h('p', {class: 'tp-note', text: 'Reopen this thread to reply.'}));
      } else if (!can('comment')) {
        replyWrap.appendChild(h('p', {class: 'tp-note', text: 'You can read this thread; commenting needs the comment role.'}));
      } else {
        ta = h('textarea', {class: 'field tp-input', rows: '1', maxlength: '2000', placeholder: 'Reply… @ to mention',
          'aria-label': 'Reply'});
        attachMentions(ta);
        if (keep) {
          ta.value = keep.value;
          var end = ta.value.length;
          try { ta.setSelectionRange(Math.min(keep.selectionStart, end), Math.min(keep.selectionEnd, end)); } catch (e) {}
          ta._mrMentions = keep.mentions;
        }
        ta.addEventListener('input', function () { typing(t.id); autoGrow(ta); });
        ta.addEventListener('keydown', function (e) {
          if (e.defaultPrevented) return;
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendReply(); }
        });
        var send = h('button', {class: 'btn btn-primary btn-round tp-send', type: 'button', 'aria-label': 'Send reply',
          title: 'Send reply', onclick: sendReply}, icon('send'));
        replyWrap.appendChild(ta);
        replyBar = composerBar(ta, {cls: 'tp-actions', keep: !!keep && !fresh, send: send});
        replyWrap.appendChild(replyBar.el);
        requestAnimationFrame(function () {
          if (!ta) return;
          autoGrow(ta);
          if (!keep || keep.focused) ta.focus({preventScroll: true});
        });
      }
      el.appendChild(replyWrap);
      renderEdit(t, list);
      renderTyping();
    }
    function renderEdit(t, list) {
      // The comment editor lives inside its own row; put it back with the text, caret and focus it had before the repaint.
      if (!p.edit) return;
      var c = (t.comments || []).filter(function (x) { return x.id === p.edit.cid; })[0];
      var row = null;
      for (var i = 0; list.children[i]; i++) if (list.children[i]._cid === p.edit.cid) row = list.children[i];
      if (!c || c.deleted_at !== null || !row) { p.edit = null; return; }
      var end0 = String(p.edit.value || '').length;
      row.textContent = '';
      var box = h('textarea', {class: 'field tp-edit', rows: '3', maxlength: '2000', 'aria-label': 'Edit comment'});
      box.value = p.edit.value;
      var end = box.value.length;
      try {
        box.setSelectionRange(Math.min(p.edit.start == null ? end0 : p.edit.start, end),
                              Math.min(p.edit.end == null ? end0 : p.edit.end, end));
      } catch (e) {}
      row.appendChild(box);
      row.appendChild(h('div', {class: 'tp-actions'}, h('span', {class: 'spacer'}),
        h('button', {class: 'btn btn-ghost', type: 'button', text: 'Cancel', onclick: function () { p.edit = null; renderPopBody(); }}),
        h('button', {class: 'btn btn-primary', type: 'button', text: 'Save', onclick: function () {
          request('comments/' + encodeURIComponent(c.id), {text: box.value}).then(function (j) {
            p.edit = null;
            if (j.thread) { upsert(j.thread); redraw(); }
            renderPopBody();
          }).catch(function (e) { opts.toast(e.message || 'Could not save', 'warn'); });
        }})));
      requestAnimationFrame(function () {
        if (box.isConnected && p.edit && p.edit.focused) box.focus({preventScroll: true});
      });
    }
    function autoGrow(input) {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight + 2, 240) + 'px';
    }
    function sendReply() {
      var text = meaningfulText(ta);
      if (!text) { if (ta) ta.focus(); return; }
      var mentions = mentionsFor(ta);
      ta.value = '';
      if (hermesAllowed()) addLeadingHermes(ta);
      if (replyBar) replyBar.sync();
      p.freshReply = true;        // the next render opens the composer in its default state
      request('threads/' + encodeURIComponent(t.id) + '/reply', {text: text, mentions: mentions}).then(function (j) {
        if (j.thread) { upsert(j.thread); redraw(); }
        renderPopBody();
        wakeNote(j);
      }).catch(function (e) { if (ta) { ta.value = text; if (replyBar) replyBar.sync(); } opts.toast(e.message || 'Could not reply', 'warn'); });
    }
    function setStatus(thread, status) {
      request('threads/' + encodeURIComponent(thread.id) + '/status', {status: status}).then(function (j) {
        if (j.thread) { upsert(j.thread); redraw(); }
        if (status === 'resolved') closePopover(); else renderPopBody();
      }).catch(function (e) { opts.toast(e.message || 'Could not update', 'warn'); });
    }
    function renderTyping() {
      var who = S.typing[p.id];
      var old = typingRow.querySelector('.typing');
      if (old) old.remove();
      if (who && who.until > Date.now()) typingRow.appendChild(h('span', {class: 'typing', text: who.name + ' is typing…'}));
      statusRow(typingRow, activityFor(p.id), hermesAllowed() && p.thread.status !== 'resolved');
    }
    p.render = renderPopBody;
    p.typing = renderTyping;
  }
  function commentRow(p, c) {
    var who = h('div', {class: 'cwho'}, h('span', {class: 'cname', text: c.author_name}),
      c.author_kind === 'guest' ? [' ', guestMark()] : null,
      c.role ? h('span', {class: 'faint', text: ' · ' + c.role}) : null,
      h('span', {class: 'faint', text: ' · ' + ago(c.created_at)}),
      c.edited_at ? h('span', {class: 'faint', text: ' · edited'}) : null);
    var text = h('div', {class: 'ctext'});
    if (c.deleted_at !== null) text.appendChild(h('span', {class: 'faint', text: 'Comment deleted'}));
    else text.appendChild(rich(c.text));
    var row = h('div', {class: 'tcomment' + (c.deleted_at !== null ? ' deleted' : '')}, avatar(c.author_name, c.author, true),
      h('div', {class: 'cbody'}, who, text));
    row._cid = c.id;
    if (c.mine && c.deleted_at === null) {
      row.appendChild(h('button', {class: 'btn btn-ghost cmore', type: 'button', 'aria-label': 'Comment actions',
        title: 'Comment actions', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
        onclick: function (e) {
          e.preventDefault();
          e.stopPropagation();
          showPopMenu(this, 'Comment actions', [
            {icon: 'pencil', text: 'Edit', run: function () { editComment(p, c); }},
            {icon: 'trash', text: 'Delete', danger: true, run: function () { deleteComment(p, c); }}]);
        }}, icon('more')));
    }
    return row;
  }
  function editComment(p, c) {
    // Open this comment's editor through the popover's own render, so a live repaint keeps it (text, caret, focus).
    p.edit = {cid: c.id, value: c.text, start: String(c.text || '').length, end: String(c.text || '').length,
      focused: true};
    if (p.render) p.render();
  }
  function deleteComment(p, c) {
    request('comments/' + encodeURIComponent(c.id) + '/delete', {}).then(function (j) {
      if (j.thread) { upsert(j.thread); redraw(); p.render(); } else closePopover();
    }).catch(function (e) { opts.toast(e.message || 'Could not delete', 'warn'); });
  }
  function markRead(t) {
    if (!t.unread) return;
    t.unread = false;
    S.counts.unread = Math.max(0, (S.counts.unread || 0) - 1);
    syncHeader();
    request('threads/' + encodeURIComponent(t.id) + '/read', {}).catch(function () {});
  }

  // ---- entries for the board's own pin layers --------------------------
  function docPoint(v) {   // a bounded page (document) point: finite px only
    if (!v || typeof v !== 'object' || typeof v.x !== 'number' || typeof v.y !== 'number') return null;
    if (!isFinite(v.x) || !isFinite(v.y)) return null;
    return {x: Math.min(Math.max(v.x, 0), 1000000), y: Math.min(Math.max(v.y, 0), 1000000)};
  }
  function scrollPoint() {
    return {x: (typeof window !== 'undefined' && (window.pageXOffset || window.scrollX)) || 0,
            y: (typeof window !== 'undefined' && (window.pageYOffset || window.scrollY)) || 0};
  }
  function pagePoint(box, x, y) {   // the page spot behind a fraction of a viewport box (the same convention as anchors.js)
    var s = scrollPoint();
    return docPoint({x: box.left + x * box.width + s.x, y: box.top + y * box.height + s.y});
  }
  function spot(t, scope, box) {
    var a = anchor(t), target = a.target ? cleanTarget(a.target) : null;
    var x = Number(a.x || 0), y = Number(a.y || 0), detached = !!t.detached, doc = null;
    if (target && scope && box && box.width && box.height) {
      var el = resolve(scope, target);
      var range = el && target.quote ? rangeFor(el, target) : null;
      var r = el && (!target.quote || range) ? (range ? range.getBoundingClientRect() : Element.prototype.getBoundingClientRect.call(el)) : null;
      if (r && r.width && r.height) {
        var fx = target.quote ? 0.5 : Math.min(1, Math.max(0, target.x));
        var fy = target.quote ? 0.5 : Math.min(1, Math.max(0, target.y));
        x = Math.min(1, Math.max(0, (r.left - box.left + fx * r.width) / box.width));
        y = Math.min(1, Math.max(0, (r.top - box.top + fy * r.height) / box.height));
        t._x = x; t._y = y;
        t._doc = pagePoint(box, x, y) || t._doc;   // where it resolved on this page, so detaching keeps the last spot
        return {x: x, y: y, detached: false, doc: null};
      }
      detached = true;
      doc = docPoint(t._doc) || docPoint(a.target.doc) || docPoint(a.doc);
    } else {
      doc = docPoint(a.target ? a.target.doc : a.doc);
    }
    if (doc) return {x: x, y: y, detached: detached, doc: doc};
    if (t._x !== undefined) { x = t._x; y = t._y; }
    return {x: x, y: y, detached: detached, doc: null};
  }
  // A resolved thread keeps its marker unless the review sets hide_resolved_pins.
  function eligible(t) {
    return t && (t.status !== 'resolved' || !(opts.hideResolved && opts.hideResolved())) && !S.hidden[t.id] && unmirrored(t);
  }
  function canvasEntries() {
    var w = opts.width();
    return S.threads.filter(function (t) {
      var a = anchor(t);
      return eligible(t) && a.on === 'board' && (!a.w || a.w === w || !!a.target);
    });
  }
  function mockEntries(block, option) {
    var w = opts.width();
    return S.threads.filter(function (t) {
      var a = anchor(t);
      return eligible(t) && !a.on && String(a.block || '') === String(block || '')
        && optionMatches(a, block, option) && (!a.w || a.w === w) && a.block !== 'proto';
    });
  }
  function optionMatches(a, block, option) {
    // An anchor with no option is still anchored to the block; it rides the mock the board shows by default
    // (recommended, else the first) instead of vanishing from the board.
    if (String(a.option || '') === String(option || '')) return true;
    if (a.option) return false;
    var first = opts.firstOption ? opts.firstOption(block) : null;
    return first !== null && first !== undefined && String(first) === String(option || '');
  }
  function protoEntries() {
    var w = opts.width();
    return S.threads.filter(function (t) {
      var a = anchor(t);
      return eligible(t) && a.block === 'proto' && (!a.w || a.w === w);
    });
  }
  function frameEntries(block, device) {
    return S.threads.filter(function (t) {
      var a = anchor(t);
      return eligible(t) && !a.on && String(a.block || '') === String(block || '') && a.w === device;
    });
  }
  function marker(t, style, detached) {
    var label = 'Thread ' + t.n + ': ' + firstText(t);
    var b = h('button', {
      class: 'mpin tpin' + (detached ? ' det' : '') + (t.unread ? ' un' : ''),
      type: 'button', style: style || null, title: detached ? 'Element removed · ' + label : label,
      'data-thread': t.id, 'aria-label': label
    }, h('span', {text: String(t.n)}));
    b.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      open(t, {detached: !!detached});
    });
    return paintPin(b, activityFor(t.id));
  }
  function row(t) {
    return h('li', {class: 'tpin-row'},
      h('button', {class: 'pin-row', type: 'button', onclick: function () { open(t); }},
        h('span', {class: 'pin-num', text: String(t.n)}),
        h('span', {class: 'pin-body'}, h('span', {class: 'pin-where', text: anchorLabel(t)}),
          h('span', {class: 'pin-text', text: firstText(t)}))));
  }

  // ---- create, edit, delete from the board's composer ----------------
  function create(pin, text, ta) {
    var body = {
      anchor: {block: pin.block, option: pin.option || null, w: pin.w || opts.width(), x: pin.x, y: pin.y,
        ...(pin.on === 'board' ? {on: 'board'} : {}),
        ...(pin.target ? {target: pin.target} : (pin.doc ? {doc: {x: pin.doc.x, y: pin.doc.y}} : {}))},
      text: text, mentions: mentionsFor(ta), idem: pin.idem || undefined
    };
    return request('threads', body).then(function (j) {
      if (j.thread) { S.created[j.thread.id] = true; upsert(j.thread); }
      syncHeader();
      redraw();
      wakeNote(j);
      return j.thread;
    });
  }
  function editThreadFirst(threadId, text) {
    var t = find(threadId);
    if (!t) return Promise.resolve(null);
    var mine = (t.comments || []).filter(function (c) { return c.mine && c.deleted_at === null; })[0];
    if (!mine) return Promise.resolve(null);
    return request('comments/' + encodeURIComponent(mine.id), {text: text}).then(function (j) {
      if (j.thread) { upsert(j.thread); redraw(); }
      return j.thread;
    });
  }
  function deleteThreadFirst(threadId) {
    var t = find(threadId);
    if (!t) return Promise.resolve(null);
    var mine = (t.comments || []).filter(function (c) { return c.mine && c.deleted_at === null; })[0];
    if (!mine) return Promise.resolve(null);
    return request('comments/' + encodeURIComponent(mine.id) + '/delete', {}).then(function (j) {
      if (j.thread) { upsert(j.thread); redraw(); } else { S.hidden[threadId] = true; S.threads = S.threads.filter(function (x) { return x.id !== threadId; }); redraw(); }
      return j.thread || null;
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Tab' && S.overlay) {
      var of = overlayFocusables(S.overlay.el);
      if (!of.length) { e.preventDefault(); return; }
      var at = document.activeElement, inside = S.overlay.el.contains(at);
      if (!inside || (e.shiftKey && at === of[0]) || (!e.shiftKey && at === of[of.length - 1])) {
        e.preventDefault();
        (e.shiftKey ? of[of.length - 1] : of[0]).focus();
      }
      return;
    }
    if (e.key === 'Tab' && S.pop && S.pop.el.classList.contains('sheet')) {
      var f = Array.prototype.filter.call(S.pop.el.querySelectorAll('button,textarea,input,a[href]'), function (el) {
        return !el.disabled && el.offsetParent !== null;
      });
      if (f.length) {
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
      return;
    }
    if (e.key !== 'Escape') return;
    if (S.menu) { e.preventDefault(); e.stopPropagation(); closePopMenu(); return; }
    if (S.overlay) { e.preventDefault(); e.stopPropagation(); closeOverlay(); return; }
    if (S.pop) { e.preventDefault(); e.stopPropagation(); closePopover(); }
  }, true);
  document.addEventListener('pointerdown', function (e) {
    if (S.menu && !S.menu.el.contains(e.target) && !(S.menu.btn && S.menu.btn.contains(e.target))) closePopMenu();
    if (S.menu && S.menu.el.contains(e.target)) return;
    if (!S.pop || S.pop.el.contains(e.target)) return;
    if (e.target.closest && e.target.closest('[data-thread], [data-pin-request]')) return;
    closePopover();
  });
  window.addEventListener('pagehide', function () {
    if (S.es) { S.es.close(); S.es = null; }
    clearTimeout(deadlineTimer);
    clearInterval(elapsedTimer);
  });

  return {
    boot: boot, setView: setView, refresh: refresh, close: closePopover,
    headerActions: headerActions, workingCount: workingCount,
    roundRequests: roundRequests, working: working, activityFor: activityFor, activityState: activityState,
    paintPin: paintPin, pinActivityChanged: pinActivityChanged, openFix: openFix, statusRow: statusRow,
    canvasEntries: canvasEntries, mockEntries: mockEntries, protoEntries: protoEntries, frameEntries: frameEntries,
    spot: spot, marker: marker, row: row,
    open: open,
    gone: function (id) { return !!S.hidden[id]; },
    resolved: function (id) { var t = find(id); return !!(t && t.status === 'resolved'); },
    unread: function (id) { var t = find(id); return !!(t && t.unread); },
    create: create, editThreadFirst: editThreadFirst, deleteThreadFirst: deleteThreadFirst,
    mentionsFor: mentionsFor, attachMentions: attachMentions, composerBar: composerBar, meaningfulText: meaningfulText,
    counts: function () { return S.counts; }, threads: function () { return S.threads; }
  };
}
