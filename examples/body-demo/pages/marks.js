/* Marks kept in the page's own data.
   One object under the key "marks": { "<part id>": { "status": "approve" | "change" | "skip", "note": "..." } }.
   It survives a reload and reaches the agent in the submit under page_state[<block id>].
   Opened outside a board (no window.mockreview) it keeps the marks in memory, so the page still works on its own. */
var MarkStore = (function () {
  var hook = window.mockreview && window.mockreview.use ? window.mockreview.use('data') : null;
  var memory = {};
  return {
    load: function () {
      if (!hook) return Promise.resolve(memory);
      return hook.get('marks').then(function (v) { return v || {}; }, function () { return {}; });
    },
    save: function (marks) {
      memory = marks;
      if (!hook) return Promise.resolve(true);
      return hook.set('marks', marks).catch(function (e) { console.log(e.message); });
    }
  };
})();

/* Wire every [data-mark-group]: its Approve / Change / Skip buttons and its note field.
   onChange(marks) runs after each change and once after the saved marks are restored. */
function wireMarks(onChange) {
  var marks = {};
  var groups = Array.prototype.slice.call(document.querySelectorAll('[data-mark-group]'));
  function paint(group) {
    var m = marks[group.dataset.markGroup] || {};
    group.querySelectorAll('button[data-status]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.status === m.status ? 'true' : 'false');
    });
    var note = group.querySelector('input[data-note]');
    if (note && note !== document.activeElement) note.value = m.note || '';
  }
  function commit(id) {
    MarkStore.save(marks);
    paint(document.querySelector('[data-mark-group="' + id + '"]'));
    if (onChange) onChange(marks);
  }
  groups.forEach(function (group) {
    var id = group.dataset.markGroup;
    group.querySelectorAll('button[data-status]').forEach(function (b) {
      b.addEventListener('click', function () {
        var m = marks[id] || {};
        if (m.status === b.dataset.status) delete m.status; else m.status = b.dataset.status;
        if (m.status || m.note) marks[id] = m; else delete marks[id];
        commit(id);
      });
    });
    var note = group.querySelector('input[data-note]');
    if (note) note.addEventListener('input', function () {
      var m = marks[id] || {};
      m.note = note.value.slice(0, 280);
      if (m.status || m.note) marks[id] = m; else delete marks[id];
      MarkStore.save(marks);
      if (onChange) onChange(marks);
    });
  });
  MarkStore.load().then(function (saved) {
    marks = saved && typeof saved === 'object' ? saved : {};
    groups.forEach(paint);
    if (onChange) onChange(marks);
  });
}
