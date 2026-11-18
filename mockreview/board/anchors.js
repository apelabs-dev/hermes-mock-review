/* Shared DOM anchoring for inline shadow mocks and opaque-origin prototypes.
 * All selectors are length capped, syntax checked, queried in a guarded scope, and must be unique.
 * Highlight wrappers never receive identities; their text remains part of the original element.
 */
import {finder, quote, position} from './lib/anchors.js';

// A page may shadow an element's own methods with form controls: <form><input name="matches"> makes form.matches the
// input, and calling it throws. Every page-authored element goes through these prototype calls instead.
const E = Element.prototype, N = Node.prototype;
// Captured once: a page that replaces Element.prototype.getAttribute (or any of these) after load must not reach the
// probe's own calls. A later property lookup would find the page's function.
const matches = E.matches, closest = E.closest, getAttribute = E.getAttribute, hasAttribute = E.hasAttribute,
  setAttribute = E.setAttribute, querySelector = E.querySelector, getBoundingClientRect = E.getBoundingClientRect,
  contains = N.contains;
const getter = (proto, name) => {
  for (let p = proto; p; p = Object.getPrototypeOf(p)) { const d = Object.getOwnPropertyDescriptor(p, name); if (d && d.get) return d.get; }
  throw new Error(name);
};
// Captured once too: a page that redefines one of these getters must not reach the probe's own reads.
const nodeTypeGet = getter(N, 'nodeType'), localNameGet = getter(E, 'localName'), textGet = getter(N, 'textContent'),
  parentGet = getter(N, 'parentElement'), previousGet = getter(E, 'previousElementSibling');
const is = (el, sel) => matches.call(el, sel);
const up = (el, sel) => closest.call(el, sel);
const attr = (el, name) => getAttribute.call(el, name);
const hasAttr = (el, name) => hasAttribute.call(el, name);
const setAttr = (el, name, value) => setAttribute.call(el, name, value);
const containsNode = (el, node) => contains.call(el, node);
const nodeTypeOf = el => nodeTypeGet.call(el);
const tagOf = el => localNameGet.call(el);
const textOf = el => textGet.call(el);
const rectOf = el => getBoundingClientRect.call(el);
const parentOf = el => parentGet.call(el);
const queryIn = (el, sel) => querySelector.call(el, sel);
const previousOf = el => previousGet.call(el);

export const LIVE = 'video,audio,a,button,input,select,textarea,summary,[contenteditable],[data-mr-live]';
const OMIT = 'script,style,link,meta,head,mr-overlay,[data-mr-overlay],[data-mr-highlight]';
const text = (s, n) => typeof s === 'string' ? s.replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, n) : '';
const finite = (v, max = 1) => typeof v === 'number' && Number.isFinite(v) ? Math.min(max, Math.max(0, v)) : 0;
const object = v => v && typeof v === 'object' && !Array.isArray(v);
export function safePath(v) {
  if (typeof v !== 'string' || !v || v.length > 300 || /[\x00-\x1f\x7f{};`]/.test(v)) return '';
  // Only finder output and attribute/id/type/child paths; exclude expensive/function selectors.
  if (/:/.test(v.replace(/:nth-(?:child|of-type)\(\d+\)/g, ''))) return '';
  try { document.createDocumentFragment().querySelector(v); return v; } catch (_) { return ''; }
}
export function cleanTarget(v) {
  if (!object(v)) return null;
  const path = safePath(v.path);
  if (!path || !object(v.sig) || typeof v.sig.tag !== 'string' || typeof v.sig.h !== 'string' || !/^[a-z][a-z0-9-]{0,39}$/.test(v.sig.tag || '') || !/^[a-z0-9]{1,40}$/.test(v.sig.h || '')) return null;
  const out = {path, x: finite(v.x), y: finite(v.y), pin: {x: finite(v.pin?.x), y: finite(v.pin?.y)},
    sig: {tag: v.sig.tag, h: v.sig.h}, label: text(v.label, 120), doc: {x: finite(v.doc?.x, 1000000), y: finite(v.doc?.y, 1000000)}};
  if (text(v.id, 80)) out.id = text(v.id, 80);
  if (text(v.part, 40)) out.part = text(v.part, 40);
  if (object(v.quote) && typeof v.quote.exact === 'string' && v.quote.exact && v.quote.exact.length <= 160) {
    // Preserve exact whitespace: W3C offsets refer to raw DOM text, not collapsed labels.
    out.quote = {type: 'TextQuoteSelector', exact: v.quote.exact.replace(/\x00/g, ''),
      prefix: typeof v.quote.prefix === 'string' ? v.quote.prefix.slice(-32) : '', suffix: typeof v.quote.suffix === 'string' ? v.quote.suffix.slice(0, 32) : ''};
    if (object(v.position) && Number.isInteger(v.position.start) && Number.isInteger(v.position.end) &&
        v.position.start >= 0 && v.position.end > v.position.start && v.position.end <= 1000000) {
      out.position = {type: 'TextPositionSelector', start: v.position.start, end: v.position.end};
    }
  }
  return out;
}
function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619); return (h >>> 0).toString(36); }
function signature(el) { return {tag: tagOf(el), h: hash(tagOf(el) + ':' + text(textOf(el), 500))}; }
function eligible(el, scope) { return !!el && nodeTypeOf(el) === 1 && el !== scope && !is(el, OMIT) && !up(el, 'mr-overlay,[data-mr-overlay]'); }
function all(scope) { return Array.from(scope.querySelectorAll('*')).filter(el => eligible(el, scope)); }
const identities = new WeakMap();
export function stamp(scope) {
  // Reserve deleted identities until this surface is disposed; a new sibling must never inherit one.
  const used = identities.get(scope) || new Set();
  identities.set(scope, used);
  all(scope).forEach(el => { const id = attr(el, 'data-mr-id'); if (id) used.add(id); });
  all(scope).forEach(el => {
    if (hasAttr(el, 'data-mr-id')) return;
    const base = 'mr-' + signature(el).h; let id = base, i = 1;
    while (used.has(id)) id = base + '-' + i++;
    used.add(id); setAttr(el, 'data-mr-id', id);
  });
}
function by(scope, name, value) { return all(scope).filter(el => attr(el, name) === value); }
function one(scope, path) { try { return path ? Array.from(scope.querySelectorAll(path)).filter(el => eligible(el, scope)) : []; } catch (_) { return []; } }
function pathIn(el, scope) {
  // finder reads element properties (getAttribute, classList, parentElement) that a form's controls can shadow. Build
  // the same kind of path from prototype calls when it throws, and keep it only when it is unique and selector-safe.
  const parts = [];
  for (let node = el; node && node !== scope; node = parentOf(node)) {
    const id = attr(node, 'id');
    if (id && /^[A-Za-z][\w-]*$/.test(id)) {
      const sel = '#' + CSS.escape(id);
      if (one(scope, sel).length === 1) { parts.unshift(sel); break; }
    }
    const tag = tagOf(node); if (!tag) return '';
    let n = 1;
    for (let sib = previousOf(node); sib; sib = previousOf(sib)) if (tagOf(sib) === tag) n++;
    parts.unshift(tag + ':nth-of-type(' + n + ')');
  }
  const path = parts.join(' > ');
  return path && one(scope, path).length === 1 ? path : '';
}
// The server accepts a path only without child combinators (its own grammar in targets.safe_path). Finder may return
// one when an element is not the block's direct child, so a keyword-rich identity is the fallback: stamping gives every
// surface a stable data-mr-id, which survives a reload and a width switch.
function serverPath(v) { return typeof v === 'string' && v && v.indexOf('>') < 0 ? v : ''; }
export function describeTarget(el, scope) {
  if (!eligible(el, scope)) return null;
  const sig = signature(el), part = text(attr(el, 'data-mr-part'), 40);
  let path = part && attr(el, 'data-mr-part') === part && by(scope, 'data-mr-part', part).length === 1 ? '[data-mr-part="' + CSS.escape(part) + '"]' : '';
  if (!path) { try { path = finder(el, {root: scope, timeoutMs: 40}); } catch (_) {} }
  if (!serverPath(path)) path = pathIn(el, scope);
  if (!serverPath(path)) {
    const id = text(attr(el, 'data-mr-id'), 80);
    const alt = id && by(scope, 'data-mr-id', id).length === 1 ? '[data-mr-id="' + CSS.escape(id) + '"]' : '';
    if (alt) path = alt;
  }
  path = serverPath(path);
  path = safePath(path); if (!path) return null;
  let heading = is(el, 'h1,h2,h3,h4,h5,h6') ? el : null;
  for (let parent = el; !heading && parent; parent = parentOf(parent)) {
    heading = queryIn(parent, 'h1,h2,h3,h4,h5,h6');
    if (parent === scope) break;
  }
  const own = text(textOf(el), 40), role = text(attr(el, 'role') || attr(el, 'type') || tagOf(el), 40);
  const label = is(el, 'h1,h2,h3,h4,h5,h6') ? text(textOf(el), 120) :
    text((heading ? text(textOf(heading), 60) + ' > ' : '') + role + (own ? ' “' + own + '”' : ''), 120);
  return {path, id: text(attr(el, 'data-mr-id'), 80), ...(part ? {part} : {}), sig, label};
}
export function anchorFor(el, scope, cx, cy, range) {
  if (range) {
    el = range.commonAncestorContainer;
    if (nodeTypeOf(el) !== 1) el = parentOf(el);
  }
  const desc = describeTarget(el, scope); if (!desc) return null;
  const r = range ? range.getBoundingClientRect() : rectOf(el);
  cx = Number.isFinite(cx) ? cx : r.left + r.width / 2;
  cy = Number.isFinite(cy) ? cy : r.top + r.height / 2;
  const er = rectOf(el);
  const out = {...desc, x: finite((cx - er.left) / (er.width || 1)), y: finite((cy - er.top) / (er.height || 1)),
    pin: {x: 0, y: 0}, doc: {x: finite(cx + scrollX, 1000000), y: finite(cy + scrollY, 1000000)}};
  if (range) {
    if (!range.toString() || range.toString().length > 160 || !containsNode(el, range.startContainer) || !containsNode(el, range.endContainer)) return null;
    try { out.quote = {type: 'TextQuoteSelector', ...quote.fromRange(el, range)};
      out.position = {type: 'TextPositionSelector', ...position.fromRange(el, range)}; } catch (_) { return null; }
  }
  return cleanTarget(out);
}
export function resolve(scope, target) {
  const t = cleanTarget(target); if (!t) return null;
  // Stable identity can survive edits to the element's text. Duplicate identifiers must detach.
  for (const [attr, value] of [['data-mr-id', t.id], ['data-mr-part', t.part]]) {
    if (!value) continue;
    const hits = by(scope, attr, value); if (hits.length > 1) return null; if (hits.length === 1) return hits[0];
  }
  const hits = one(scope, t.path);
  if (hits.length > 1) return null;
  if (hits.length === 1 && signature(hits[0]).tag === t.sig.tag && signature(hits[0]).h === t.sig.h &&
      (!t.id || !hasAttr(hits[0], 'data-mr-id') || attr(hits[0], 'data-mr-id') === t.id)) return hits[0];
  const matches = all(scope).filter(el => { const s = signature(el); return s.tag === t.sig.tag && s.h === t.sig.h; });
  return matches.length === 1 ? matches[0] : null;
}
export function rangeFor(el, t) {
  if (!el || !t.quote) return null;
  const content = textOf(el), q = t.quote;
  // The quote library supports fuzzy matching. We require an exact, uniquely contextual match so ambiguity detaches.
  const candidates = []; let at = -1;
  while ((at = content.indexOf(q.exact, at + 1)) >= 0) {
    if ((!q.prefix || content.slice(Math.max(0, at - q.prefix.length), at) === q.prefix) &&
        (!q.suffix || content.slice(at + q.exact.length, at + q.exact.length + q.suffix.length) === q.suffix)) candidates.push(at);
    if (candidates.length > 1) return null;
  }
  if (candidates.length !== 1) return null;
  try {
    const start = candidates[0], r = position.toRange(el, {start, end: start + q.exact.length});
    if (r && r.toString() === q.exact) return r;
    const fallback = quote.toRange(el, q, {hint: start});
    return fallback && fallback.toString() === q.exact ? fallback : null;
  } catch (_) { return null; }
}

// One observer/hover/highlight lifecycle per surface. Caller disconnects when the surface is removed.
export function surface(scope, overlay, options) {
  let pending = 0, hoverFrame = 0, hoverPoint = null, disposed = false, marks = [], highlighted = [];
  const doc = scope.ownerDocument;
  const outline = doc.createElement('div'); outline.dataset.mrOverlay = '';
  outline.className = 'mr-hover'; outline.hidden = true;
  outline.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483646;outline:2px solid #6366f1;outline-offset:2px;border-radius:3px';
  const tag = doc.createElement('span');
  tag.className = 'mr-hover-label';
  tag.style.cssText = 'position:absolute;left:-2px;top:-24px;max-width:320px;overflow:hidden;text-overflow:ellipsis;' +
    'white-space:nowrap;background:#18181b;color:#fff;font:600 11px/16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
    'padding:1px 7px;border-radius:5px;pointer-events:none';
  outline.appendChild(tag);
  doc.documentElement.appendChild(outline);
  const style = doc.createElement('style'); style.textContent = '::highlight(mr-comments){background:#fde68a;color:#18181b}mark[data-mr-highlight]{background:#fde68a;color:#18181b}';
  scope.appendChild(style);
  stamp(scope);
  const native = !!(globalThis.CSS?.highlights && globalThis.Highlight);
  // Shadow roots require styles in their own tree; the registry itself belongs to the document.
  if (native && !CSS.highlights.has('mr-comments')) CSS.highlights.set('mr-comments', new Highlight());
  function clearHighlights() {
    if (native) { const registry = CSS.highlights.get('mr-comments'); highlighted.forEach(r => registry?.delete(r)); }
    if (!native) marks = Array.from(scope.querySelectorAll('mark[data-mr-highlight]'));
    marks.forEach(mark => { if (mark.parentNode) { const p = mark.parentNode; mark.replaceWith(...mark.childNodes); p.normalize(); } });
    marks = []; highlighted = [];
  }
  function showRange(range) {
    if (native) { CSS.highlights.get('mr-comments').add(range); highlighted.push(range); return; }
    // Wrap individual text slices: never extract/reparent native controls or invalidate stamped identities.
    const walk = doc.createTreeWalker(scope, NodeFilter.SHOW_TEXT), nodes = [];
    while (walk.nextNode()) {
      const node = walk.currentNode, parent = parentOf(node);
      if (range.intersectsNode(node) && textOf(node) && !(parent && up(parent, 'style,script'))) nodes.push(node);
    }
    nodes.forEach(node => {
      const r = doc.createRange(); r.selectNodeContents(node);
      if (node === range.startContainer) r.setStart(node, range.startOffset);
      if (node === range.endContainer) r.setEnd(node, range.endOffset);
      if (r.collapsed) return;
      const mark = doc.createElement('mark'); mark.dataset.mrHighlight = ''; r.surroundContents(mark); marks.push(mark);
    });
  }
  function refresh() {
    pending = 0; if (disposed || !scope.isConnected) return;
    observer.disconnect(); clearHighlights(); stamp(scope);
    const watched = new Set([scope]);
    const before = JSON.stringify(options.pins());
    const base = options.box();
    (options.pins() || []).forEach(p => {
      const t = cleanTarget(p.target); if (!t) return; // old and empty-space pins retain their coordinates
      const el = resolve(scope, t), range = t.quote ? rangeFor(el, t) : null;
      const rect = el && (!t.quote || range) ? (range ? range.getBoundingClientRect() : rectOf(el)) : null;
      p.detached = !rect;
      if (!p.detached && rect.width && rect.height) {
        const er = t.quote ? rect : rectOf(el);
        const cx = er.left + (t.quote ? 0.5 : t.x) * er.width, cy = er.top + (t.quote ? 0.5 : t.y) * er.height;
        p.x = finite((cx - base.left) / (base.width || 1) + t.pin.x);
        p.y = finite((cy - base.top) / (base.height || 1) + t.pin.y);
        t.doc = {x: finite(base.left + p.x * base.width + scrollX, 1000000), y: finite(base.top + p.y * base.height + scrollY, 1000000)};
        p.target = t;
        watched.add(el);
        if (range) showRange(range);
      }
    });
    options.draw();
    if (options.changed && before !== JSON.stringify(options.pins())) options.changed();
    observer.observe(scope, {subtree: true, childList: true, characterData: true, attributes: true});
    observed.forEach(el => { if (!watched.has(el)) { resize.unobserve(el); observed.delete(el); } });
    watched.forEach(el => { if (!observed.has(el)) { resize.observe(el); observed.add(el); } });
  }
  function schedule() { if (!pending && !disposed) pending = requestAnimationFrame(refresh); }
  const observer = new MutationObserver(schedule);
  const resize = new ResizeObserver(schedule);
  const observed = new Set();
  function hover(e) {
    hoverPoint = e;
    if (hoverFrame) return;
    hoverFrame = requestAnimationFrame(() => {
      hoverFrame = 0;
      if (!options.enabled()) return clearHover();
      const el = scope.getRootNode().elementFromPoint?.(hoverPoint.clientX, hoverPoint.clientY) || hoverPoint.target;
      if (!eligible(el, scope) || !containsNode(scope, el)) return clearHover();
      const r = rectOf(el);
      const desc = describeTarget(el, scope);
      tag.textContent = desc ? desc.label : '';
      tag.hidden = !desc || !desc.label;
      tag.style.top = r.top > 28 ? '-24px' : 'auto';
      tag.style.bottom = r.top > 28 ? 'auto' : '-24px';
      outline.hidden = false; Object.assign(outline.style, {left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px'});
    });
  }
  function clearHover() { outline.hidden = true; }
  scope.addEventListener('pointermove', hover); scope.addEventListener('pointerleave', clearHover);
  addEventListener('scroll', schedule, true); addEventListener('resize', schedule);
  schedule();
  return {schedule, clearHover, dispose() {
    disposed = true; cancelAnimationFrame(pending); cancelAnimationFrame(hoverFrame); observer.disconnect(); resize.disconnect(); clearHighlights();
    scope.removeEventListener('pointermove', hover); scope.removeEventListener('pointerleave', clearHover);
    removeEventListener('scroll', schedule, true); removeEventListener('resize', schedule); outline.remove(); style.remove();
  }};
}

// A small page hook: shell owns the composer and network; pages may only describe anchors.
export function installAPI(scopes, open, enabled) {
  const api = window.mockreview && typeof window.mockreview === 'object' ? window.mockreview : {};
  let custom = null;
  function locate(el) { return scopes().find(scope => scope.isConnected && el && containsNode(scope, el)); }
  function make(request) {
    const {element, range} = object(request) ? request : {};
    if (range && !(range instanceof Range)) return null;
    if (!range && !(element instanceof Element)) return null;
    const node = range ? range.commonAncestorContainer : element, scope = locate(node);
    return scope ? anchorFor(element, scope, undefined, undefined, range) : null;
  }
  api.comments = {
    anchorFor(element) { return make({element}); },
    registerAnchors(fn) { custom = typeof fn === 'function' ? fn : null; },
    customAnchors() {
      try { const list = custom ? custom() : []; return Array.isArray(list) ? list.slice(0, 60).map(cleanTarget).filter(Boolean) : []; } catch (_) { return []; }
    },
    openComposer(request) { const target = make(request); if (!target || !enabled()) return null; open(target, request); return target; }
  };
  // The probe's hook is non-writable, so a plain assignment silently does nothing in sloppy mode and throws in strict.
  if (window.mockreview !== api) window.mockreview = api;
}
