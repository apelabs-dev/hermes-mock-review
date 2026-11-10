"""Format 1.2 board logic, executed from the production board.js in Node: question marks, the gate rule, the progress
line, grouping, "Approve the rest", the build payload and the question / piece / settled renderers."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'mockreview/board/board.js').read_text(encoding='utf-8')
NAMES = ['h', 'append', 'paras', 'entryKind', 'capCustom', 'isResolved', 'gateState', 'progressLine', 'isMarkableBlock',
         'groupBlocks', 'approveRestIds', 'groupHasBulk', 'gateIcon', 'endLine', 'icon', 'updateGate', 'gateReason', 'cleanMarks', 'answerText', 'payload', 'renderQuestion', 'renderPiece',
         'renderSettled', 'renderAsked', 'renderNotesAnswered', 'markEmpty', 'submittedFeedback', 'watchWindowMs']


def run(test):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for executed board DOM tests')
    parts = [re.search(r'^  var LIMIT = .*$', SRC, re.M).group(0), "var PROGRESS = " + re.search(r'^  var PROGRESS = (.*);$', SRC, re.M).group(1) + ";",
             "var NOTE_CHIP = " + re.search(r'^  var NOTE_CHIP = (.*);$', SRC, re.M).group(1) + ";",
             "var WATCH_EVERY = " + re.search(r'^  var WATCH_EVERY = (.*);$', SRC, re.M).group(1) + ";",
             "var GATE_BTN = " + re.search(r"^  var GATE_BTN = ([^;]*);", SRC, re.M).group(1) + ";"]
    for name in NAMES:
        start = re.search(r'^  function ' + name + r'\(', SRC, re.M)
        assert start, name
        end = SRC.find('\n  function ', start.end())
        parts.append(SRC[start.start():end])
    res = subprocess.run([node, str(ROOT / 'tests/board_dom.js')], input=json.dumps(
        {'functions': '\n'.join(parts), 'trees': {}, 'test': test}), capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=20)
    assert res.returncode == 0, res.stderr


INDEX = '''[{id:'p1',kind:'piece',group:'A'},{id:'p2',kind:'piece',group:'A'},{id:'q1',kind:'question',group:'Q'},
 {id:'d1',kind:'decision',group:'A'},{id:'defaults',kind:'defaults',group:''}]'''


def test_gate_not_ready_reason_text_and_ready():
    run('const I=' + INDEX + ''';
const g = {label: 'Start building'};
let s = gateState(I, {}, g);
assert.equal(s.ready, false); assert.equal(s.reason, '5 still open');
s = gateState(I, {p1: {status: 'approve'}, p2: {status: 'change'}, q1: {choice: 'recommended'}}, g);
assert.equal(s.reason, '2 still open · 1 on Change'); assert.deepEqual(s.changed, ['p2']);
s = gateState(I, {p1: {status: 'approve'}, p2: {status: 'change'}, q1: {choice: 'recommended'}, d1: {status: 'skip'}, defaults: {status: 'approve'}}, g);
assert.equal(s.reason, '1 on Change'); assert.equal(s.ready, false);
const all = {p1: {status: 'approve'}, p2: {status: 'skip'}, q1: {choice: 'custom', custom: 'x'}, d1: {status: 'approve'}, defaults: {status: 'approve'}};
s = gateState(I, all, g); assert.equal(s.ready, true); assert.equal(s.reason, '');
assert.equal(gateState(I, all, null).ready, false);
// a custom answer with no text is not an answer; a note alone does not resolve a question
assert.equal(gateState(I, Object.assign({}, all, {q1: {choice: 'custom', custom: '  '}}), g).ready, false);
assert.equal(gateState(I, Object.assign({}, all, {q1: {note: 'hm'}}), g).open[0], 'q1');
''')


def test_progress_line_and_old_kinds():
    run('const I=' + INDEX + ''';
assert.equal(progressLine(I, {}), '0/3 plan · 0/1 questions · 0/1 picks');
assert.equal(progressLine(I, {p1: {status: 'approve'}, q1: {choice: 'recommended'}}), '1/3 plan · 1/1 questions · 0/1 picks');
// rounds stored before 1.2 have no kind: a decision, a recap by layout
assert.equal(entryKind({id: 'd', layout: 'side'}), 'decision'); assert.equal(entryKind({id: 'r', layout: 'recap'}), 'recap');
assert.equal(progressLine([{id: 'd', layout: 'side'}, {id: 'r', layout: 'recap'}], {d: {status: 'skip'}}), '1/1 picks · 0/1 to confirm');
''')


def test_custom_answer_cap_and_clean_marks():
    run('''
assert.equal(capCustom('x'.repeat(400)).length, 280);
assert.equal(capCustom('a\\nb\\tc'), 'a b c');
const m = cleanMarks({q1: {choice: 'custom', custom: '   ', note: ''}, q2: {choice: 'custom', custom: '  hi   there '}, q3: {choice: 'recommended', custom: 'stale'}, q4: {choice: 'custom', custom: ' ', note: 'n'}});
assert.equal(m.q1, undefined); assert.equal(m.q2.custom, 'hi there'); assert.equal(m.q3.custom, undefined);
assert.equal(m.q4.choice, null); assert.equal(m.q4.note, 'n');
''')


def test_build_payload_carries_action_and_send_stays_plain():
    run('''
const d = {marks: {q1: {choice: 'recommended'}}, pins: [], guidelines: ''};
assert.equal(payload(d, 'build').action, 'build');
assert.equal(payload(d, 'send').action, 'send');
assert.equal('action' in payload(d), false);
assert.deepEqual(payload(d, 'build').marks.q1, {choice: 'recommended'});
''')


def test_group_blocks_and_approve_rest():
    run('''
const blocks = [{type: 'text'}, {type: 'settled'}, {type: 'defaults', id: 'defaults'},
 {type: 'question', id: 'q1', group: 'Q'}, {type: 'piece', id: 'p1', group: 'A'}, {type: 'callout'},
 {type: 'piece', id: 'p2', group: 'A'}, {type: 'decision', id: 'd1'}, {type: 'recap', id: 'r', markable: false}];
const g = groupBlocks(blocks);
assert.equal(g.lead.length, 1);
assert.deepEqual(g.groups.map(x => x.name), ['Q', 'A', '']);
assert.deepEqual(g.groups[1].blocks.map(b => b.type), ['piece', 'callout', 'piece']);
assert.equal(g.groups[2].blocks.length, 2);   // d1 and the non-markable recap that follows it
const I = [{id: 'q1', kind: 'question', group: 'Q'}, {id: 'p1', kind: 'piece', group: 'A'}, {id: 'p2', kind: 'piece', group: 'A'}, {id: 'p3', kind: 'piece', group: 'A'}, {id: 'defaults', kind: 'defaults', group: 'A'}];
assert.deepEqual(approveRestIds(I, 'A', {p1: {status: 'change'}, p2: {note: 'x'}}), ['p2', 'p3']);   // Change is left alone, a note-only block is still open
assert.deepEqual(approveRestIds(I, 'Q', {}), []);   // questions are never approved in bulk
''')


def test_question_renderer():
    run('''
global.S = {draft: {marks: {}}}; global.isEditable = () => true; global.mark = id => S.draft.marks[id] || {status: null, option: null, note: ''};
global.requestAnimationFrame = () => {}; global.autoGrow = () => {}; global.setMarkQuiet = () => {}; global.setMark = () => {}; global.pickAnswer = () => {}; global.setCustom = () => {};
const b = {id: 'q1', title: 'T', text: 'Why?', recommendation: {answer: '30 days', why: 'Bounded.'}};
const kids = renderQuestion(b);
const root = document.createElement('div'); append(root, kids);
const t = root.textContent;
for (const s of ['Why?', 'Recommendation', '30 days', 'Bounded.', 'Go with recommendation', 'Different answer']) assert.ok(t.includes(s), s);
const input = root.querySelectorAll('input')[0];
assert.equal(input.getAttribute('maxlength'), '280');
assert.equal(root.querySelectorAll('.qcustom')[0].hidden, true);
assert.ok(root.querySelectorAll('.qcount')[0].textContent.endsWith('/ 280'));
''')


def test_piece_asked_settled_and_notes_renderers():
    run('''
global.S = {draft: {marks: {}}}; global.isEditable = () => true; global.mark = () => ({}); global.requestAnimationFrame = () => {};
global.icon = () => document.createElement('svg'); global.autoGrow = () => {}; global.setMarkQuiet = () => {}; global.setMark = () => {};
global.decideRow = () => document.createElement('div'); global.mockBox = (r, a, b, o) => { global.boxed = o; return document.createElement('div'); };
const nodes = renderPiece({}, {}, {id: 'p1', text: 'Body', items: ['one', 'two'], html: '<b>x</b>'});
const root = document.createElement('div'); append(root, nodes);
assert.equal(root.querySelectorAll('li').length, 2); assert.equal(boxed.html, '<b>x</b>'); assert.equal(boxed.id, null);
assert.equal(renderPiece({}, {}, {id: 'p2'}).filter(Boolean).length, 2);   // ipins + decide row only: no text, items or mock
assert.equal(renderAsked({}), null);
const qa = renderAsked({asked: 'Q?', answer: 'A.'});
assert.ok(qa.textContent.includes('You asked') && qa.textContent.includes('Q?') && qa.textContent.includes('Answer'));
const st = renderSettled({items: [{title: 'T', quote: 'said', rule: 'does'}, {title: 'U'}]});
assert.equal(st.querySelectorAll('button').length, 0); assert.equal(st.querySelectorAll('blockquote').length, 1);
assert.ok(st.textContent.includes('Already decided') && st.textContent.includes('does'));
const na = renderNotesAnswered({blocks: [{id: 'p2', title: 'Conflicts'}], notes_answered: [
 {ref: 'p2', note: 'n', answer: 'a', status: 'second-look'}, {ref: 'p2', note: 'n', answer: 'a', status: 'needs-pick'}, {ref: 'p2', note: 'n', answer: 'a', status: 'done'}]});
assert.deepEqual(na.querySelectorAll('.na-chip').map(c => c.textContent), ['Needs a second look', 'Needs a pick', 'Done']);
assert.equal(renderNotesAnswered({blocks: [], notes_answered: []}), null);
''')


def test_group_has_bulk_only_for_actionable_kinds():
    run('''
const I = [{id: 'q1', kind: 'question', group: 'Q'}, {id: 'q2', kind: 'question', group: 'Q'}, {id: 'defaults', kind: 'defaults', group: 'Q'},
 {id: 'p1', kind: 'piece', group: 'A'}, {id: 'd1', kind: 'decision', group: ''}, {id: 'r', kind: 'recap', group: 'R'}, {id: 'pr', kind: 'prototype', group: 'P'}];
assert.equal(groupHasBulk(I, 'Q'), false);   // question-only (plus defaults): nothing to approve in bulk
assert.equal(groupHasBulk(I, 'A'), true); assert.equal(groupHasBulk(I, ''), true); assert.equal(groupHasBulk(I, 'R'), true);
assert.equal(groupHasBulk(I, 'P'), false); assert.equal(groupHasBulk(I, 'none'), false); assert.equal(groupHasBulk(null, 'A'), false);
assert.equal(groupHasBulk([{id: 'd', layout: 'side'}], ''), true);   // old rounds have no kind: a decision
''')


def test_gate_icon_rule():
    run('''
assert.equal(gateIcon(true, true), 'send'); assert.equal(gateIcon(false, true), 'lock');
assert.equal(gateIcon(true, false), 'lock'); assert.equal(gateIcon(false, false), 'lock');
''')


def test_update_gate_swaps_the_icon_with_the_marks():
    run('''
global.ICONS = {lock: '', send: ''};
const btn = document.createElement('button'); btn.setAttribute('id', 'gate-btn');
btn.appendChild(icon('lock')); const lab = document.createElement('span'); lab.setAttribute('class', 'gate-label'); btn.appendChild(lab);
const rs = document.createElement('span'); rs.setAttribute('id', 'gate-reason');
const root = document.createElement('div'); root.appendChild(btn); root.appendChild(rs); global.ROOT_NODE = root;
const I = [{id: 'p1', kind: 'piece', group: 'A'}, {id: 'q1', kind: 'question', group: 'Q'}];
const g = {label: 'Start building'}; let ed = true;
global.S = {draft: {marks: {}}, sending: false}; global.DOC = {dec: {gate: g}}; global.viewed = () => ({decisions: I, gate: g}); global.isEditable = () => ed;
const ico = () => btn.querySelectorAll('svg')[0].getAttribute('data-icon');
updateGate(); assert.equal(ico(), 'lock'); assert.equal(btn.querySelectorAll('svg').length, 1);
S.draft.marks = {p1: {status: 'approve'}, q1: {choice: 'recommended'}};
updateGate(); assert.equal(ico(), 'send'); assert.equal(btn.children[0].tagName, 'SVG'); assert.equal(btn.querySelectorAll('svg').length, 1);
assert.equal(rs.textContent, 'Nothing is open or on Change. Press Build below.'); assert.ok(rs.classList.contains('ready'));
assert.equal(lab.textContent, 'Build');   // short label: it shares the footer slot with Send notes
S.sending = true; updateGate(); assert.equal(ico(), 'send'); assert.equal(lab.textContent, 'Sending…');   // stays on send while sending
S.sending = false; ed = false; updateGate(); assert.equal(ico(), 'lock');
S.draft.marks = {p1: {status: 'change'}, q1: {choice: 'recommended'}}; ed = true; updateGate(); assert.equal(ico(), 'lock');
''')


NO_GATE = '''const I = [{id: 'p1', kind: 'piece'}, {id: 'q1', kind: 'question'}, {id: 'd1', kind: 'decision'}];
const all = {p1: {status: 'approve'}, q1: {choice: 'recommended'}, d1: {status: 'skip'}};
const NX = 'The sync engine comes first.';'''


def test_end_line_without_a_gate_keeps_today_s_strings():
    run(NO_GATE + '''
const part = {p1: {status: 'approve'}, q1: {note: 'x'}};   // a note alone leaves the question open
assert.equal(endLine(I, {}, null, '', true), '3 of 3 still open.');
assert.equal(endLine(I, part, null, '', false), '2 of 3 still open.');
assert.equal(endLine(I, part, null, NX, true), '2 of 3 still open. Once all are decided → ' + NX);
assert.equal(endLine(I, all, null, '', true), 'All 3 decided. Press Send when you\u2019re ready.');
assert.equal(endLine(I, all, null, NX, true), 'All 3 decided. Next → ' + NX + ' Press Send when you\u2019re ready.');
assert.equal(endLine(I, all, null, '', false), 'All 3 decided.');
assert.equal(endLine(I, all, null, NX, false), 'All 3 decided. Next → ' + NX);
// a block on Change counts as decided without a gate, as it always did
assert.equal(endLine(I, Object.assign({}, all, {p1: {status: 'change'}}), null, '', false), 'All 3 decided.');
''')


def test_end_line_with_a_gate_never_repeats_the_call_to_action():
    run(NO_GATE + '''
const g = {label: 'Start building'};
assert.equal(endLine(I, {}, g, '', true), '3 of 3 still open.');
assert.equal(endLine(I, {p1: {status: 'approve'}}, g, NX, true), '2 of 3 still open. Once all are decided → ' + NX);
assert.equal(endLine(I, {p1: {status: 'change'}, q1: {choice: 'recommended'}}, g, '', true), '1 of 3 still open · 1 on Change.');
assert.equal(endLine(I, {p1: {status: 'change'}}, g, NX, true), '2 of 3 still open · 1 on Change. Once all are decided → ' + NX);
// nothing open, but one block is on Change: not "All decided"
const chg = Object.assign({}, all, {p1: {status: 'change'}});
assert.equal(endLine(I, chg, g, '', true), '1 of 3 on Change.');
assert.equal(endLine(I, chg, g, NX, true), '1 of 3 on Change. Once all are decided → ' + NX);
for (const ed of [true, false]) {
  assert.equal(endLine(I, all, g, '', ed), 'All 3 decided.');
  assert.equal(endLine(I, all, g, NX, ed), 'All 3 decided. Next → ' + NX);
}
for (const s of [endLine(I, all, g, NX, true), endLine(I, {}, g, NX, true), endLine(I, chg, g, NX, true)]) assert.ok(!/Press|ready/.test(s), s);
''')


def test_watch_window_stretches_for_start_building():
    run('''
global.R = () => ({current_round: 9, id: 'r'});
global.localSent = () => null;
assert.equal(WATCH_EVERY, 30000);
assert.equal(watchWindowMs({n: 2, feedback: {action: 'build'}}), 8 * 3600 * 1000);
assert.equal(watchWindowMs({n: 2, feedback: {action: 'send'}}), 2 * 3600 * 1000);
assert.equal(watchWindowMs({n: 2, feedback: {}}), 2 * 3600 * 1000);
assert.equal(watchWindowMs({n: 2}), 2 * 3600 * 1000);   // no feedback yet: the ordinary window
''')


def test_gate_card_has_no_button_and_its_reason_still_updates():
    run('''
global.ICONS = {lock: '', send: '', check: ''};
const row = document.createElement('div'); row.appendChild(icon('lock'));
const rs = document.createElement('span'); rs.setAttribute('id', 'gate-reason'); row.appendChild(rs);
global.ROOT_NODE = row;
const I = [{id: 'p1', kind: 'piece', group: 'A'}, {id: 'q1', kind: 'question', group: 'Q'}];
const g = {label: 'Start building'};
global.S = {draft: {marks: {}}, sending: false}; global.DOC = {dec: {gate: g}}; global.viewed = () => ({decisions: I, gate: g}); global.isEditable = () => true;
updateGate(); assert.ok(/still open/.test(rs.textContent), rs.textContent); assert.ok(!rs.classList.contains('ready'));
assert.equal(row.children[0].getAttribute('data-icon'), 'lock');
S.draft.marks = {p1: {status: 'approve'}, q1: {choice: 'recommended'}};
updateGate(); assert.equal(rs.textContent, 'Nothing is open or on Change. Press Build below.'); assert.ok(rs.classList.contains('ready'));
assert.equal(row.children[0].getAttribute('data-icon'), 'check'); assert.equal(row.children.length, 2);
''')
