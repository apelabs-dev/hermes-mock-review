/* One deterministic colour per person for every avatar on the board.
 *
 * The key is the stable actor id (`user:<id>`, `guest:<id>`) so a guest who renames keeps the colour and only the
 * initials change. A name-only caller still gets a stable colour from the trimmed, lower-cased name. The index is an
 * FNV-1a hash of that key, so a reload (and the other theme) paints the same person the same way. No colours live
 * here: the eight fills and their inks are CSS rules keyed by `data-av`.
 */
'use strict';

export function avatarKey(actor, name) {
  var a = actor === null || actor === undefined ? '' : String(actor).trim();
  if (a) return a;
  var n = String(name === null || name === undefined ? '' : name).trim().toLowerCase();
  return n || '?';
}

export function avatarIndex(actor, name) {
  var key = avatarKey(actor, name), h = 2166136261;
  for (var i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % 8;
}

export function paintAvatar(el, actor, name) {
  if (!el) return -1;
  var index = avatarIndex(actor, name);
  el.setAttribute('data-av', String(index));
  return index;
}
