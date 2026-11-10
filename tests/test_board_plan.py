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
         'groupBlocks', 'approveRestIds', 'cleanMarks', 'answerText', 'payload', 'renderQuestion', 'renderPiece',
         'renderSettled', 'renderAsked', 'renderNotesAnswered', 'markEmpty']


def run(test):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for executed board DOM tests')
    parts = [re.search(r'^  var LIMIT = .*$', SRC, re.M).group(0), "var PROGRESS = " + re.search(r'^  var PROGRESS = (.*);$', SRC, re.M).group(1) + ";",
             "var NOTE_CHIP = " + re.search(r'^  var NOTE_CHIP = (.*);$', SRC, re.M).group(1) + ";"]
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
