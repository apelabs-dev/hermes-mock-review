// Small dependency-free DOM for isolated board functions. HTML attributes are parsed
// by Python's HTMLParser (including character references), never by a regex in JS.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Node {
  constructor(tag = '', text = '') { this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this.attrs = {}; this.text = text; this.style = {}; }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({name, value})); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(n) { if (n.parentNode) n.remove(); this.children.push(n); n.parentNode = this; return n; }
  get firstChild() { return this.children[0] || null; }
  remove() { if (this.parentNode) { const a = this.parentNode.children; a.splice(a.indexOf(this), 1); this.parentNode = null; } }
  replaceWith(n) { const p = this.parentNode; const i = p.children.indexOf(this); this.remove(); p.children.splice(i, 0, n); n.parentNode = p; }
  set textContent(v) { this.children = []; this.text = String(v); }
  get textContent() { return this.text + this.children.map(n => n.textContent).join(''); }
  matches(sel) { return sel.split(',').some(s => s === '*' ? !!this.tagName : s === '.mpin' ? (this.attrs.class || '').split(' ').includes('mpin') : s === 'a[href]' ? this.tagName === 'A' && this.getAttribute('href') !== null : s.toUpperCase() === this.tagName); }
  querySelectorAll(sel) { return this.children.flatMap(n => [...(n.matches(sel) ? [n] : []), ...n.querySelectorAll(sel)]); }
  closest(sel) { return this.matches(sel) ? this : this.parentNode ? this.parentNode.closest(sel) : null; }
  addEventListener(name, cb) { (this.listeners ||= {})[name] = cb; }
}
function fromTree(tree) {
  const n = new Node(tree.tag || '', tree.text || '');
  for (const [k, v] of tree.attrs || []) n.setAttribute(k, v || '');
  for (const child of tree.children || []) n.appendChild(fromTree(child));
  return n;
}
global.Node = Node;
global.document = {
  createElement(tag) {
    const el = new Node(tag);
    if (tag === 'template') {
      Object.defineProperty(el, 'innerHTML', {set(html) {
        assert.ok(Object.hasOwn(input.trees, html), 'HTML fixture must be parsed first');
        this.content = fromTree(input.trees[html]);
      }});
    }
    return el;
  },
  createTextNode(text) { return new Node('', text); }
};
vm.runInThisContext(input.functions);
// Only test code is evaluated here; fixtures stay JSON data.
const test = new Function('assert', input.test);
test(assert);
