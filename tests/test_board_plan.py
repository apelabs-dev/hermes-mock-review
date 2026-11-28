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
         'groupBlocks', 'approveRestIds', 'groupHasBulk', 'gateIcon', 'endLine', 'icon', 'updateGate', 'gateReason', 'gateTitle', 'renderGate', 'updateSendbar', 'cleanMarks', 'payload', 'renderQuestion', 'renderPiece',
         'renderSettled', 'renderRecapNote', 'renderAsked', 'renderNotesAnswered', 'renderBlock', 'artifactDisplayClass',
         'savedCopy', 'markEmpty', 'submittedFeedback', 'watchWindowMs',
         'newPinId', 'uuid', 'isEl', 'closestOf', 'num', 'scrollPoint', 'docPoint', 'pct', 'sectionOf', 'sectionRef',
         'canvasBox', 'canvasClick', 'nextPinNo']


def run(test):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for executed board DOM tests')
    parts = [re.search(r'^  var LIMIT = .*$', SRC, re.M).group(0), "var PROGRESS = " + re.search(r'^  var PROGRESS = (.*);$', SRC, re.M).group(1) + ";",
             "var NOTE_CHIP = " + re.search(r'^  var NOTE_CHIP = (.*);$', SRC, re.M).group(1) + ";",
             "var WATCH_EVERY = " + re.search(r'^  var WATCH_EVERY = (.*);$', SRC, re.M).group(1) + ";",
             "var GATE_BTN = " + re.search(r"^  var GATE_BTN = ([^;]*);", SRC, re.M).group(1) + ";",
             "var PIN_SKIP = " + re.search(r"^  var PIN_SKIP = (.*);$", SRC, re.M).group(1) + ";",
             "var SECTIONS = new Map();"]
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
const g = {label: 'Build'};
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


def test_payload_carries_pin_ids():
    run('''
const d = {marks: {}, pins: [{id: 'p1a2b3c4', block: 'm1', option: null, w: 'desktop', x: 0.1, y: 0.2, text: 'fix'}], guidelines: ''};
const out = payload(d, 'send');
assert.equal(out.pins[0].id, 'p1a2b3c4');
assert.equal(out.pins[0].block, 'm1');
const bare = {marks: {}, pins: [{block: 'm1', option: null, w: 'desktop', x: 0.1, y: 0.2, text: 'fix'}], guidelines: ''};
assert.equal('id' in payload(bare, 'send').pins[0], false);
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
const root = document.createElement('div'); root.appendChild(btn); global.ROOT_NODE = root;
const I = [{id: 'p1', kind: 'piece', group: 'A'}, {id: 'q1', kind: 'question', group: 'Q'}];
const g = {label: 'Build', text: 'Work starts with sync.'}; let ed = true;
global.S = {draft: {marks: {}}, sending: false}; global.DOC = {dec: {gate: g}}; global.viewed = () => ({decisions: I, gate: g}); global.isEditable = () => ed;
const ico = () => btn.querySelectorAll('svg')[0].getAttribute('data-icon');
updateGate(); assert.equal(ico(), 'lock'); assert.equal(btn.querySelectorAll('svg').length, 1); assert.equal(btn.title, '2 still open');   // the reason lives in the tooltip
S.draft.marks = {p1: {status: 'approve'}, q1: {choice: 'recommended'}};
updateGate(); assert.equal(ico(), 'send'); assert.equal(btn.children[0].tagName, 'SVG'); assert.equal(btn.querySelectorAll('svg').length, 1);
assert.equal(btn.title, 'Build: Work starts with sync.');
assert.equal(lab.textContent, 'Build');   // short label: it shares the footer slot with Send
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
const g = {label: 'Build'};
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


def test_footer_button_is_send_or_build_never_both():
    run('''
global.ICONS = {lock: '', send: '', check: ''};
const I = [{id: 'p1', kind: 'piece', group: 'A'}, {id: 'q1', kind: 'question', group: 'Q'}];
const g = {label: 'Build'};
const round = {n: 1, decisions: I, gate: g};
let sent = [], toasts = []; let ed = true;
global.sendbar = document.createElement('footer'); global.footMsg = null;
global.S = {draft: {marks: {}}, sending: false, amending: false, error: null}; global.DOC = {dec: {gate: g}};
global.ownerSends = () => true;
global.R = () => ({status: 'open', current_round: 1, submit: {kind: 'local'}}); global.viewed = () => round;
global.isNew = () => true; global.isEditable = () => ed; global.submittedFeedback = () => null;
global.missingNotes = () => []; global.hasContent = d => Object.keys(d.marks).length > 0;
global.setFootMsg = () => {}; global.summaryChips = () => [];
global.send = a => sent.push(a || 'send'); global.toast = m => toasts.push(m);
const all = () => Array.from(sendbar.querySelectorAll('button'));
const only = () => { assert.equal(all().length, 1); return all()[0]; };   // one button, whatever the state
const press = () => only().listeners.click.call(only());
const DONE = {p1: {status: 'approve'}, q1: {choice: 'recommended'}};

global.footerButton = () => 'build';
updateSendbar(); assert.equal(only().textContent.trim(), 'Build'); assert.equal(only().getAttribute('aria-disabled'), 'true');
assert.equal(only().getAttribute('title'), '2 still open');
press(); assert.deepEqual(sent, []); assert.deepEqual(toasts, ['2 still open']);   // locked: a tap says why and sends nothing
S.draft.marks = {p1: {status: 'approve'}}; updateSendbar(); assert.equal(only().getAttribute('title'), '1 still open');
S.draft.marks = DONE; updateSendbar();
assert.equal(only().textContent.trim(), 'Build'); assert.equal(only().getAttribute('aria-disabled'), 'false');
press(); assert.deepEqual(sent, ['build']);
S.draft.marks = {p1: {status: 'change', note: 'split it'}, q1: {choice: 'recommended'}}; updateSendbar();
assert.equal(only().textContent.trim(), 'Send'); press(); assert.deepEqual(sent, ['build', 'send']);   // on Change: Send takes the slot

global.footerButton = () => 'send'; S.draft.marks = DONE; updateSendbar(); assert.equal(only().textContent.trim(), 'Send');
global.footerButton = () => 'build'; ed = false; updateSendbar(); assert.notEqual(only().textContent.trim(), 'Build');   // a locked round never offers Build
round.gate = null; ed = true; updateSendbar(); assert.equal(only().textContent.trim(), 'Send');                          // no gate: Send
''')


def test_only_the_owner_gets_send_or_build_in_the_footer():
    copy = re.search(r"^  var OWNER_ONLY_COPY = '([^']*)';$", SRC, re.M).group(1)
    assert copy == "Only the owner sends this round to Hermes. Your comments and pins are on the board for them."
    run('''
global.ICONS = {lock: '', send: '', check: ''};
global.OWNER_ONLY_COPY = %s;
const I = [{id: 'p1', kind: 'piece', group: 'A'}, {id: 'q1', kind: 'question', group: 'Q'}];
const g = {label: 'Build'};
const round = {n: 1, decisions: I, gate: g};
let sent = [], toasts = [];
global.sendbar = document.createElement('footer'); global.footMsg = null;
global.S = {draft: {marks: {p1: {status: 'approve'}, q1: {choice: 'recommended'}}}, sending: false, amending: false,
            error: null};
global.DOC = {dec: {gate: g}};
global.R = () => ({status: 'open', current_round: 1, submit: {kind: 'local'}}); global.viewed = () => round;
global.isNew = () => true; global.isEditable = () => true; global.submittedFeedback = () => null;
global.missingNotes = () => []; global.hasContent = d => Object.keys(d.marks).length > 0;
global.setFootMsg = () => {}; global.summaryChips = () => [];
global.send = a => sent.push(a || 'send'); global.toast = m => toasts.push(m);
const buttons = () => Array.from(sendbar.querySelectorAll('button'));
global.ownerSends = () => false;   // a share-link guest or a member, Reviewer role included: not the owner
for (const slot of ['build', 'send']) {
  global.footerButton = () => slot; updateSendbar();
  assert.equal(buttons().length, 0, slot);                          // no Send, no Build, ready to build or not
  assert.equal(sendbar.querySelectorAll('.sb-owner-only').length, 1, slot);
  assert.equal(sendbar.querySelector('.sb-owner-only').textContent, OWNER_ONLY_COPY);
  assert.ok(sendbar.classList.contains('sb-reader'));
}
assert.deepEqual(sent, []);
global.ownerSends = () => true;    // the owner keeps the one button
global.footerButton = () => 'build'; updateSendbar();
assert.equal(buttons().length, 1); assert.equal(buttons()[0].textContent.trim(), 'Build');
assert.equal(sendbar.querySelectorAll('.sb-owner-only').length, 0);
''' % json.dumps(copy))


def test_the_gate_has_no_card_only_its_lists():
    run('''
const r = {n: 1};
assert.equal(renderGate(r, {gate: {label: 'Build', text: 'Approves the plan.', after: [], later: []}}), null);
const sec = renderGate(r, {gate: {label: 'Build', text: 'Approves the plan.', after: ['Pair two devices'], later: ['Accounts']}});
assert.equal(sec.querySelectorAll('button').length, 0); assert.equal(sec.querySelectorAll('h2').length, 0);
assert.deepEqual(Array.from(sec.querySelectorAll('h3')).map(x => x.textContent), ['What I need from you after approval', 'Later, not in this plan']);
''')


def test_a_recap_that_is_not_markable_is_drawn_as_a_plain_list():
    run('''
const el = renderRecapNote({id: 'recap', title: 'Agreed so far', items: ['Dark theme stays', 'Header at 48 px']});
assert.equal(el.querySelectorAll('li').length, 2); assert.equal(el.querySelectorAll('button').length, 0);
assert.ok(el.textContent.includes('Agreed so far') && el.textContent.includes('Header at 48 px'));
assert.ok(renderRecapNote({id: 'recap', items: ['x']}).textContent.includes('Recap'));
''')
    run('''
const el = renderBlock(null, null, {type: 'recap', id: 'recap', markable: false, items: ['x', 'y']});
assert.equal(el.querySelectorAll('li').length, 2);
assert.equal(el.querySelectorAll('button').length, 0);
''')


def test_the_saved_copy_says_a_wake_off_board_does_not_ping_hermes():
    run('''
global.ownerSends = () => true;
assert.equal(savedCopy({wake: true}, 'Sent - Hermes is on it'), 'Sent - Hermes is on it');
assert.equal(savedCopy({wake: false}, 'Sent - Hermes is on it'), 'Saved. This board does not ping Hermes: tell it in chat.');
assert.equal(savedCopy(null, 'old'), 'old');
''')


def test_canvas_pin_skips_controls_and_records_section():
    run('''
    global.window = { crypto: globalThis.crypto };
global.S = {commentMode: true, width: 'desktop', draft: {pins: []}};
global.T = {threads: () => []};
global.isEditable = () => true; global.pinsOn = () => true; global.viewed = () => ({}); global.canComment = () => true;
global.canvasBox = () => ({left: 0, top: 0, width: 200, height: 100});
global.wlabel = w => w; global.anchorFor = () => null;
global.canvasLayer = () => ({layer: document.createElement('div'), list: document.createElement('ol')});
global.saveDraft = () => {}; global.redrawCanvasPins = () => {}; global.toast = () => {};
// Saving a canvas pin also posts a thread. The thread POST has its own tests; here the seam is stubbed so this
// test keeps checking exactly what it checked before: the ref, the geometry and the pin's shape.
global.saveNewThread = (pin, text) => { pin.text = text; S.draft.pins.push(pin); };
let popped = null;
global.notePop = o => { popped = o; if (o.onClose) o.onClose(); if (o.onSave) o.onSave('too vague'); };
function sec(ref, title) { const s = document.createElement('section'); s.setAttribute('data-mr-ref', ref); s.setAttribute('data-mr-title', title); return s; }
function click(el, x, y) { canvasClick({clientX: x === undefined ? 50 : x, clientY: y === undefined ? 50 : y, composedPath: () => [el], target: el}); }
const s2 = sec('s2', 'Rollout plan');
const button = document.createElement('button'); s2.appendChild(button);
const area = document.createElement('textarea'); s2.appendChild(area);
const input = document.createElement('input'); s2.appendChild(input);
const link = document.createElement('a'); s2.appendChild(link);
const mpin = document.createElement('div'); mpin.setAttribute('class', 'mpin'); s2.appendChild(mpin);
const mock = document.createElement('div'); mock.setAttribute('class', 'mock');
const inner = document.createElement('p'); mock.appendChild(inner); s2.appendChild(mock);
for (const el of [button, area, input, link, mpin, inner]) click(el);
assert.equal(S.draft.pins.length, 0);
assert.equal(sectionRef(s2), 's2');
assert.equal(sectionRef(button), 's2');
const outside = document.createElement('div');
assert.equal(sectionRef(outside), '');
click(s2);
assert.equal(S.draft.pins.length, 1);
assert.ok(/^p[0-9a-f]{8}$/.test(S.draft.pins[0].id));
delete S.draft.pins[0].id;
assert.deepEqual(S.draft.pins, [{block: 's2', option: null, w: 'desktop', x: 0.25, y: 0.5, text: 'too vague', on: 'board', doc: {x: 50, y: 50}}]);
assert.equal(popped.title, 'New pin · Rollout plan');
assert.equal(popped.where, 'desktop · ~25%, 50%');
const head = sec('page', 'Board');
click(head, 100, 20);
assert.equal(S.draft.pins.length, 2);
assert.equal(S.draft.pins[1].block, 'page');
assert.equal(S.draft.pins[1].x, 0.5); assert.equal(S.draft.pins[1].y, 0.2);
''')
