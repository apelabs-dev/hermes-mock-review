import { paintAvatar } from '/_/avatar.js';
/* mock-review Settings. Admins: Members, Invites. Everyone with an account: Devices.
   Sections are a side list on desktop and a horizontal tab row on a phone; each setting is one row with its
   label, a one-line help and the control on the right. Every POST sends the mr_csrf cookie's value in X-CSRF-Token.
   Text from the server goes in with textContent. */
(function () {
  'use strict';

  var app = document.getElementById('app');
  var SOURCES = { loopback: 'this computer', invite: 'invite', login: 'sign-in link', tailscale: 'tailnet',
                  cloudflare: 'Cloudflare Access', forwarded: 'sign-in proxy' };

  function h(tag, props) {
    var el = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid === null || kid === undefined || kid === false) continue;
      (Array.isArray(kid) ? kid : [kid]).forEach(function (c) {
        if (c !== null && c !== undefined && c !== false) el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
      });
    }
    return el;
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)mr_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function api(method, path, body) {
    return fetch('/api/' + path, {
      method: method, credentials: 'same-origin', cache: 'no-store',
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() } : {},
      body: method === 'POST' ? JSON.stringify(body || {}) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (!res.ok || j.ok === false) throw new Error(j.error || ('The board answered HTTP ' + res.status));
        return j;
      });
    });
  }

  function initials(name) {
    return (String(name || '').trim().split(/\s+/).map(function (w) { return w.charAt(0); }).join('').slice(0, 2) || '?').toUpperCase();
  }
  function day(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '';
  }
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 120) return 'now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }
  function device(ua) {
    ua = String(ua || '');
    var os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
      : /Mac OS X|Macintosh/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : '';
    var br = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome'
      : /Safari\//.test(ua) ? 'Safari' : '';
    return [os, br].filter(Boolean).join(' · ') || 'Unknown device';
  }

  var errBox = h('div', { class: 'err', role: 'status' });
  function fail(e) { errBox.textContent = e.message || String(e); }
  function clearErr() { errBox.textContent = ''; }

  function avatar(name, warn, actor) {
    var el = h('span', { class: 'av' + (warn ? ' warn' : ''), 'aria-hidden': 'true', text: initials(name) });
    paintAvatar(el, actor, name);
    return el;
  }
  function settingRow(label, help, control, extra) {
    return h('div', { class: 'setting-row' },
      h('div', { class: 'setting-main' }, label, help ? h('span', { class: 'setting-help', text: help }) : null),
      extra || null,
      h('div', { class: 'setting-control' }, control));
  }
  function paneHead(title, sub) {
    return h('div', { class: 'pane-head' }, h('h2', { text: title }), sub ? h('p', { class: 'setting-help', text: sub }) : null);
  }

  // ---- Members
  function members(pane) {
    return api('GET', 'users').then(function (j) {
      var rows = j.users.map(function (u) {
        var src = (u.sources && u.sources.length ? u.sources : [u.source]).map(function (s) { return SOURCES[s] || s; }).join(', ');
        var sel = h('select', { class: 'sel', 'aria-label': 'Role of ' + u.name, disabled: u.locked },
          h('option', { value: 'member', text: 'Member' }), h('option', { value: 'admin', text: 'Admin' }));
        sel.value = u.role;
        sel.addEventListener('change', function () {
          clearErr();
          api('POST', 'users/' + u.id + '/role', { role: sel.value }).then(function () { return members(pane); }, function (e) {
            fail(e); sel.value = u.role;
          });
        });
        return settingRow(
          h('span', { class: 'row-title' }, avatar(u.name, u.role !== 'admin', u.id ? 'user:' + u.id : ''), h('span', { class: 'row-name', text: u.name }),
            u.you ? h('span', { class: 'badge', text: 'You' }) : null),
          (u.you ? 'you · ' : '') + src + (u.locked ? ' · last admin' : ''),
          sel);
      });
      pane.replaceChildren(paneHead('Members', 'Who can open reviews on this board.'),
        h('div', { class: 'setting-list' }, rows));
    });
  }

  // ---- Invites
  function invites(pane) {
    var label = h('input', { class: 'inp', placeholder: 'Who is it for? (optional)', maxlength: 60, 'aria-label': 'Who the invite is for' });
    var out = h('div', { class: 'setting-link', hidden: true });
    var gen = h('button', { class: 'btn pri', type: 'button', text: 'Generate invite' });
    gen.addEventListener('click', function () {
      clearErr();
      gen.disabled = true;
      api('POST', 'invites', { role: 'member', label: label.value }).then(function (j) {
        var url = location.origin + j.path;
        out.replaceChildren(h('span', { class: 'link', text: url, title: url }));
        out.hidden = false;
        label.value = '';
        var copied = navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject();
        return copied.then(function () { gen.textContent = 'Copied'; }, function () { gen.textContent = 'Generate invite'; });
      }).then(function () { return list(); }, fail).then(function () {
        gen.disabled = false;
        setTimeout(function () { gen.textContent = 'Generate invite'; }, 2000);
      });
    });
    var listBox = h('div', { class: 'setting-list' });
    function list() {
      return api('GET', 'invites').then(function (j) {
        var rows = j.invites.filter(function (i) { return i.state === 'open'; }).map(function (i) {
          var revoke = h('button', { class: 'btn', type: 'button', text: 'Revoke' });
          revoke.addEventListener('click', function () {
            clearErr();
            api('POST', 'invites/' + i.id + '/revoke').then(list, fail);
          });
          return settingRow(
            h('span', { class: 'row-title' }, h('span', { class: 'row-name', text: i.label ? 'for “' + i.label + '”' : 'invite #' + i.id })),
            'expires ' + day(i.expires_at), revoke);
        });
        listBox.replaceChildren.apply(listBox, rows.length ? rows
          : [h('p', { class: 'empty', text: 'No open invites.' })]);
      });
    }
    pane.replaceChildren(
      paneHead('Invites', 'Single-use links for people who need an account.'),
      h('div', { class: 'setting-row' },
        h('div', { class: 'setting-main' }, h('span', { class: 'row-title' }, h('span', { class: 'row-name', text: 'New invite' })),
          h('span', { class: 'setting-help', text: 'Expires in 7 days' })),
        h('div', { class: 'setting-control invite-control' }, label, gen)),
      out,
      h('div', { class: 'pane-sec', text: 'Open invites' }), listBox);
    return list();
  }

  // ---- Devices
  function devices(pane, me) {
    return api('GET', 'sessions').then(function (j) {
      var rows = j.sessions.map(function (s) {
        return settingRow(
          h('span', { class: 'row-title' }, h('span', { class: 'row-name', text: device(s.device) }),
            s.current ? h('span', { class: 'badge ok', text: 'This device' }) : null),
          ago(s.last_seen), h('span', { class: 'setting-help', text: s.current ? 'current' : 'signed in' }));
      });
      if (j.this_device && j.this_device.source === 'loopback') {
        rows.unshift(settingRow(
          h('span', { class: 'row-title' }, h('span', { class: 'row-name', text: 'This computer' }),
            h('span', { class: 'badge ok', text: 'This device' })),
          'signed in by being here', h('span', { class: 'setting-help', text: 'current' })));
      }
      var others = j.sessions.filter(function (s) { return !s.current; }).length;
      var btn = h('button', { class: 'btn danger', type: 'button', disabled: !others, text: 'Sign out other devices' });
      btn.addEventListener('click', function () {
        clearErr();
        api('POST', 'sessions/sign-out-others').then(function () { return devices(pane, me); }, fail);
      });
      pane.replaceChildren(paneHead('Devices', 'Where this account is signed in.'),
        h('div', { class: 'setting-list' }, rows),
        h('div', { class: 'pane-actions' }, btn));
    });
  }

  function signOut() {
    api('POST', 'logout').then(function () { location.href = '/'; }, fail);
  }

  api('GET', 'me').then(function (j) {
    var me = j.me;
    var admin = me.role === 'admin';
    var tabs = (admin ? [['Members', members], ['Invites', invites]] : []).concat([['Devices', devices]]);
    var bar = h('div', { class: 'settings-nav', role: 'tablist', 'aria-label': 'Settings sections' });
    var panes = tabs.map(function (t, i) {
      var pane = h('div', { class: 'settings-pane', role: 'tabpanel' });
      var b = h('button', { class: 'settings-tab', type: 'button', role: 'tab', text: t[0] });
      b.addEventListener('click', function () { show(i); });
      b.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return;
        e.preventDefault();
        var next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1
          : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        panes[next].button.focus();
        show(next);
      });
      bar.appendChild(b);
      return { button: b, pane: pane, load: t[1] };
    });
    function show(i) {
      clearErr();
      panes.forEach(function (p, k) {
        p.button.classList.toggle('on', k === i);
        p.button.setAttribute('aria-selected', k === i ? 'true' : 'false');
        p.pane.classList.toggle('on', k === i);
      });
      history.replaceState(null, '', '#' + tabs[i][0].toLowerCase());
      panes[i].load(panes[i].pane, me).catch(fail);
    }
    var out = me.source === 'loopback' ? null
      : h('button', { class: 'btn', type: 'button', text: 'Sign out', onclick: signOut });
    var head = h('header', { class: 'settings-bar' },
      h('a', { class: 'brand-link', href: '/', 'aria-label': 'Mock review reviews', title: 'Reviews' },
        h('img', { class: 'brand-mark', src: '/_/favicon.svg', alt: '', 'aria-hidden': 'true' })),
      h('div', { class: 'settings-title' },
        h('strong', { text: 'Settings' }),
        h('span', { text: me.name + ' · ' + (admin ? 'admin' : 'member') })),
      h('span', { class: 'settings-spacer' }),
      out,
      h('a', { class: 'btn back', href: '/', text: 'Reviews' }));
    app.replaceChildren(head,
      h('div', { class: 'settings-body' }, bar, h('div', { class: 'settings-content' }, panes.map(function (p) { return p.pane; }), errBox)));
    var want = (location.hash || '').slice(1);
    var start = tabs.map(function (t) { return t[0].toLowerCase(); }).indexOf(want);
    show(start < 0 ? 0 : start);
  }, function (e) {
    app.replaceChildren(h('p', { class: 'muted', text: e.message }));
  });
})();
