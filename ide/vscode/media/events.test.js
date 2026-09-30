'use strict';

/**
 * Tests for the pure half of `media/events.js`: an event, as lines.
 *
 * Two things are being checked here, and the second is the one that matters.
 *
 * The first is that the wording is the wording: a tool's own `summary` first,
 * the verb from `app.js`'s `VERBS` table, the same `ago`/`took`/`flat` the rest
 * of the project uses. Drift between this renderer and the browser one is
 * invisible until somebody trusts the wrong one.
 *
 * The second is coverage of the union in `openmirror/protocol/agent.py`. Every
 * `type` the daemon can send is read out of that file and fed to `eventLines`,
 * and the assertion is that none of them comes back as an unknown event. When
 * somebody adds `tool.interrupted` to the daemon, this test fails and says so,
 * rather than the panel quietly dropping a line forever. The reverse assertion
 * is there too: a type nobody has heard of *does* produce the unknown line,
 * because swallowing it is the one behaviour `PROTOCOL.md` rules out.
 *
 * No DOM anywhere. `createTranscript` is deliberately not imported.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  compactedText, countDiff, describeCall, eventLines, firstLine, isTrivialArgument,
  policyNotices, receipt, rememberable, taskEnded, todoLines, toolHead, toolLine,
  unknownEvent, verbFor,
} from './events.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const AGENT = path.join(here, '..', '..', '..', 'openmirror', 'protocol', 'agent.py');

/** Every `type: Literal['...']` the daemon's *server -> client* models declare. */
function daemonEventTypes() {
  const text = fs.readFileSync(AGENT, 'utf8');
  const before = text.indexOf('Server -> client');
  const after = text.indexOf('Client -> server');
  assert.ok(before > 0 && after > before, 'agent.py no longer has the two sections where they were');
  const section = text.slice(before, after);
  const found = [...section.matchAll(/type:\s*Literal\['([^']+)'\]/g)].map((match) => match[1]);
  assert.ok(found.length > 10, `only found ${found.length} event types in agent.py`);
  return found;
}

const textOf = (lines) => lines.map((line) => line.text);
const classes = (lines) => lines.map((line) => line.cls);

// ---------------------------------------------------------------- the coverage

test('every event type in the AgentEvent union is rendered, not swallowed', () => {
  const types = daemonEventTypes();
  for (const type of types) {
    const lines = eventLines({ type });
    const unknown = lines.filter((line) => line.cls === 'unknown');
    assert.deepEqual(unknown, [], `${type} came back as: ${unknownEvent(type)}`);
  }
});

test('the three the daemon sends outside the union are handled too', () => {
  // `turn.queued` is what a message sent during a turn comes back as, and the
  // panel's send button stays live during a turn precisely because of it.
  assert.deepEqual(eventLines({ type: 'turn.queued', waiting: 2 }), [
    { cls: 'meta', text: 'held - it runs when this turn finishes (2 waiting)' },
  ]);
  // `text.flush` is the browser client's own batching hint: nothing to say.
  assert.deepEqual(eventLines({ type: 'text.flush' }), []);
  // `pong` is a keepalive.
  assert.deepEqual(eventLines({ type: 'pong' }), []);
});

test('an event nobody has heard of says so, and says which one', () => {
  assert.deepEqual(eventLines({ type: 'tool.interrupted', call_id: 'c1' }), [
    { cls: 'unknown', text: 'unknown event: tool.interrupted' },
  ]);
  assert.equal(unknownEvent('tool.interrupted'), 'unknown event: tool.interrupted');
  assert.equal(unknownEvent(''), 'unknown event: (no type)');
  assert.equal(unknownEvent(undefined), 'unknown event: (no type)');
  assert.equal(unknownEvent(null), 'unknown event: (no type)');
  // A number is still readable rather than "undefined".
  assert.equal(unknownEvent(7), 'unknown event: 7');
});

test('a malformed event cannot throw', () => {
  for (const bad of [null, undefined, {}, { type: null }, { type: 'tool.completed' }]) {
    assert.doesNotThrow(() => eventLines(bad));
  }
});

// ----------------------------------------------------------------- tool calls

test('a tool line is the verb, then what it was for', () => {
  assert.equal(verbFor('read_file'), 'Read');
  assert.equal(verbFor('shell'), 'Ran');
  assert.equal(verbFor('web_search'), 'Searched the web for');
  // A tool this build has never heard of is shown as its own name.
  assert.equal(verbFor('teleport'), '');
  assert.equal(toolLine({ name: 'teleport', summary: 'the cat' }), 'teleport  the cat');
  assert.equal(toolLine({}), 'tool');
});

test('describeCall: the tool writes the summary, because only it knows', () => {
  // The tool's own one-liner wins over its arguments, always.
  assert.equal(describeCall({ name: 'shell', summary: 'pytest -q', arguments: { command: 'rm -rf /' } }), 'pytest -q');
  // With no summary, the argument that matters, in the order tui.py uses.
  assert.equal(describeCall({ name: 'shell', arguments: { run_in_background: false, command: 'ls' } }), 'ls');
  assert.equal(describeCall({ name: 'edit_file', arguments: { file_path: 'a/b.py' } }), 'a/b.py');
  // A shell command arrives as a list, and it reads better joined.
  assert.equal(describeCall({ name: 'shell', arguments: { command: ['git', 'status'] } }), 'git status');
  // `.` and `/` are how a tool says "here" and say nothing.
  assert.equal(describeCall({ name: 'list_dir', arguments: { path: '.' } }), '');
  assert.equal(describeCall({ name: 'read_file', arguments: { path: '/' } }), '');
  // Nothing to say is an empty string, not "null" or "{}".
  assert.equal(describeCall({ name: 'x', arguments: {} }), '');
  assert.equal(describeCall({ name: 'x' }), '');
  assert.equal(describeCall(null), '');
  // A long one is cut, because a card's head is one line.
  assert.ok(describeCall({ name: 'x', summary: 'y'.repeat(200) }).length <= 90);
});

test('isTrivialArgument: "says nothing" is not the same as "is false"', () => {
  for (const nothing of [null, undefined, '', '  ', '.', './', '/', '~', [], {}]) {
    assert.equal(isTrivialArgument(nothing), true, `${JSON.stringify(nothing)} should say nothing`);
  }
  // `run_in_background: false` is a real argument. Treating it as nothing is
  // how a shell call ends up described by whatever is left over.
  for (const something of ['ls', 0, false, true, ['git'], { cwd: '.' }]) {
    assert.equal(isTrivialArgument(something), false, `${JSON.stringify(something)} should count`);
  }
});

test('toolHead: the pieces a card head is built from', () => {
  const head = toolHead({ id: 'c1', name: 'read_file', summary: 'src/app.js', risk: 'read' });
  assert.deepEqual(head, {
    name: 'read_file',
    known: true,
    verb: 'Read',
    summary: 'src/app.js',
    risk: 'read',
  });
  const unknown = toolHead({ name: 'mystery', risk: 'write' });
  assert.equal(unknown.known, false);
  assert.equal(unknown.verb, '');
  // A call with no risk at all is a read: that is the daemon's own default.
  assert.equal(toolHead({ name: 'x' }).risk, 'read');
});

test('a destructive call cannot be remembered, and one otherwise can', () => {
  assert.equal(rememberable('destructive'), false);
  assert.equal(rememberable('execute'), true);
  assert.equal(rememberable('read'), true);
  assert.equal(rememberable(undefined), true);
});

test('tool.proposed says the call, and says that it is waiting for a person', () => {
  const lines = eventLines({
    type: 'tool.proposed',
    needs_approval: true,
    call: { id: 'c1', name: 'shell', summary: 'rm -rf build', risk: 'destructive' },
  });
  assert.deepEqual(lines, [
    { cls: 'tool', text: 'Ran  rm -rf build' },
    { cls: 'warn', text: 'needs your approval' },
  ]);
  // The policy already allowed it, so nothing is being asked.
  assert.deepEqual(eventLines({ type: 'tool.proposed', needs_approval: false, call: { name: 'read_file' } }), [
    { cls: 'tool', text: 'Read' },
  ]);
});

test('tool.started says nothing, because the proposal already said it', () => {
  assert.deepEqual(eventLines({ type: 'tool.started', call_id: 'c1' }), []);
});

test('tool.output.delta is one line, because a build is not 40k lines of UI', () => {
  assert.deepEqual(eventLines({ type: 'tool.output.delta', text: 'first\nsecond\n' }), [
    { cls: 'tool-out', text: 'first' },
  ]);
  assert.deepEqual(eventLines({ type: 'tool.output.delta', text: '   \n' }), []);
  assert.deepEqual(eventLines({ type: 'tool.output.delta' }), []);
});

test('tool.completed: the result, the time, and whether it failed', () => {
  assert.deepEqual(
    eventLines({ type: 'tool.completed', result: { id: 'c1', name: 'read_file', ok: true, content: 'x' } }),
    [{ cls: 'tool-ok', text: 'x' }],
  );
  // A result with no content still gets a line, from whatever it displayed.
  assert.deepEqual(
    eventLines({ type: 'tool.completed', result: { id: 'c1', ok: true, display: { path: 'a.py' } } }),
    [{ cls: 'tool-ok', text: 'a.py' }],
  );
  assert.deepEqual(
    eventLines({ type: 'tool.completed', result: { id: 'c1', ok: true } }),
    [{ cls: 'tool-ok', text: 'ok' }],
  );
  assert.deepEqual(
    eventLines({
      type: 'tool.completed',
      result: { id: 'c1', ok: false, content: 'No such file', duration_ms: 1500, truncated: true },
    }),
    [{ cls: 'tool-err', text: 'failed: No such file - 1.5s - truncated' }],
  );
  // A failure with nothing to say still says it failed.
  assert.deepEqual(
    eventLines({ type: 'tool.completed', result: { id: 'c1', ok: false } }),
    [{ cls: 'tool-err', text: 'failed: no detail' }],
  );
});

test('tool.denied says it did not run, and why', () => {
  assert.deepEqual(eventLines({ type: 'tool.denied', call_id: 'c1', reason: 'not that path' }), [
    { cls: 'tool-err', text: 'not run: not that path' },
  ]);
  assert.deepEqual(eventLines({ type: 'tool.denied', call_id: 'c1' }), [{ cls: 'tool-err', text: 'not run' }]);
});

test('a question is the question and its options, in that order', () => {
  const lines = eventLines({
    type: 'question.asked',
    question: 'Which one?',
    options: ['the first', 'the second'],
  });
  assert.deepEqual(classes(lines), ['question', 'option', 'option']);
  assert.deepEqual(textOf(lines), ['Which one?', 'the first', 'the second']);
  // A question with no options is one line.
  assert.deepEqual(classes(eventLines({ type: 'question.asked', question: 'Why?' })), ['question']);
});

// ------------------------------------------------------------------ the rest

test('text and thinking are passed through untouched', () => {
  assert.deepEqual(eventLines({ type: 'text.delta', text: 'Hello  there\n' }), [
    { cls: 'delta', text: 'Hello  there\n' },
  ]);
  assert.deepEqual(eventLines({ type: 'text.delta' }), [{ cls: 'delta', text: '' }]);
  assert.deepEqual(eventLines({ type: 'thinking.delta', text: 'hmm' }), [{ cls: 'thinking', text: 'hmm' }]);
});

test('turn.started echoes what was asked, and a turn with no text says nothing', () => {
  assert.deepEqual(eventLines({ type: 'turn.started', text: 'do the thing' }), [
    { cls: 'user', text: 'do the thing' },
  ]);
  assert.deepEqual(eventLines({ type: 'turn.started', turn_id: 't1' }), []);
});

test('session.started says the promise once, and nothing without one', () => {
  assert.deepEqual(eventLines({ type: 'session.started', policy: 'reads run; everything else is asked' }), [
    { cls: 'meta', text: 'reads run; everything else is asked' },
  ]);
  assert.deepEqual(eventLines({ type: 'session.started', model: 'x' }), []);
});

test('policy.changed is not a transcript line: the mode picker already says it', () => {
  assert.deepEqual(eventLines({ type: 'policy.changed', mode: 'trusted' }), []);
});

test('policyNotices only says what actually moved', () => {
  const before = { mode: 'ask', effort: 'low', model: 'claude-sonnet-4' };
  // Nothing moved: no sentence about a control that did not change.
  assert.deepEqual(policyNotices(before, { mode: 'ask', effort: 'low', model: 'claude-sonnet-4' }), []);
  // A thinking change is not announced as a mode change.
  assert.deepEqual(policyNotices(before, { mode: 'ask', effort: 'high' }), ['Thinking is now: high.']);
  // `/model` moves the provider too, and the sentence says where.
  assert.deepEqual(
    policyNotices(before, { mode: 'ask', effort: 'low', model: 'gpt-5', provider: 'openai' }),
    ['Answering with gpt-5 on openai from now on.'],
  );
  // A mode change with no sentence of its own still says the mode's own name.
  assert.deepEqual(policyNotices(before, { mode: 'trusted', effort: 'low' }), ['Approval is now: trusted.']);
  assert.deepEqual(
    policyNotices(before, { mode: 'trusted', effort: 'low', policy: 'anything goes' }),
    ['Approval is now: anything goes.'],
  );
  // The model's own default is said as such rather than as an empty level.
  assert.deepEqual(policyNotices(before, { mode: 'ask', effort: null }), ["Thinking is now: the model's own default."]);
  // A field the event does not carry counts as cleared, which is what `app.js`
  // does with its `?? null`. The daemon always sends both, so this only arises
  // from a hand-built event -- and reading an omitted field as "cleared" is the
  // safe direction: the alternative is a panel that holds a level the session
  // no longer has.
  assert.deepEqual(
    policyNotices({ mode: 'ask', effort: 'low' }, { mode: 'ask' }),
    ["Thinking is now: the model's own default."],
  );
  // A panel that has heard nothing yet has nothing to compare against, so the
  // first policy event says the mode. The thinking level is not mentioned,
  // because "unknown" and "the model's own default" are the same thing here and
  // a sentence about a level that was never set is a sentence about nothing.
  assert.deepEqual(policyNotices({}, { mode: 'ask' }), ['Approval is now: ask.']);
  assert.deepEqual(policyNotices(undefined, undefined), []);
});

test('turn.completed says something only when it did not end normally', () => {
  assert.deepEqual(eventLines({ type: 'turn.completed', stop_reason: 'end_turn' }), []);
  assert.deepEqual(eventLines({ type: 'turn.completed', stop_reason: 'interrupted' }), [
    { cls: 'meta', text: 'Interrupted.' },
  ]);
  assert.deepEqual(eventLines({ type: 'turn.completed', stop_reason: 'max_steps' }), [
    { cls: 'error', text: 'Stopped: too many steps.' },
  ]);
  assert.deepEqual(eventLines({ type: 'turn.completed', stop_reason: 'error' }), [
    { cls: 'error', text: 'The turn ended in an error.' },
  ]);
  // No stop_reason at all is the same as a normal end.
  assert.deepEqual(eventLines({ type: 'turn.completed' }), []);
});

test('the receipt is only said when both ends of it are known', () => {
  // The daemon's clock on both sides, because a reattached panel replays turns
  // that ended hours ago and timing them against `now` reports a millisecond.
  assert.deepEqual(receipt({ at: 105 }, 100), [{ cls: 'meta', text: 'Worked for 5.0s' }]);
  assert.deepEqual(receipt({ at: 100 }, 100), []);
  assert.deepEqual(receipt({ at: 90 }, 100), []);
  assert.deepEqual(receipt({}, 100), []);
  assert.deepEqual(receipt({ at: 105 }, 0), []);
});

test('an error is shown as it arrived, flattened and clipped', () => {
  assert.deepEqual(eventLines({ type: 'error', message: 'the model is on fire' }), [
    { cls: 'error', text: 'the model is on fire' },
  ]);
  assert.equal(eventLines({ type: 'error', message: 'x'.repeat(400) })[0].text.length, 200);
  assert.deepEqual(eventLines({ type: 'error' }), [{ cls: 'error', text: '' }]);
});

test('background work says how it ended', () => {
  assert.equal(
    taskEnded({ kind: 'shell', id: 'b1', status: 'done', exit_code: 0, label: 'dev server' }),
    'Background command b1 exited with code 0: dev server',
  );
  assert.equal(
    taskEnded({ kind: 'shell', id: 'b1', status: 'stopped', exit_code: 143, label: 'dev server' }),
    'Background command b1 was stopped: dev server',
  );
  assert.equal(
    taskEnded({ kind: 'agent', id: 'b2', status: 'stopped', label: 'a search' }),
    'Background agent b2 was stopped: a search',
  );
  assert.match(taskEnded({ kind: 'agent', id: 'b3', status: 'done', label: 'x' }), /finished/);
  assert.match(taskEnded({ kind: 'agent', id: 'b4', status: 'failed', label: 'x' }), /failed/);
  // A malformed task must not produce a line with holes in it.
  assert.equal(taskEnded(null), 'Background command failed:');
});

test('a compaction says what went, and a clear says the agent starts again', () => {
  assert.equal(
    compactedText({ reason: 'automatic', messages_before: 40, messages_after: 2, tokens_before: 98000 }),
    'compacted to make room: 40 messages, about 98.0k tokens, now a summary',
  );
  assert.equal(compactedText({ reason: 'manual', messages_before: 12 }), 'compacted: 12 messages, now a summary');
  assert.equal(compactedText({ reason: 'cleared' }), 'context cleared - the agent starts afresh from here');
  assert.equal(compactedText({}), 'compacted: 0 messages, now a summary');
});

test('session.ended says the reason', () => {
  assert.deepEqual(eventLines({ type: 'session.ended', reason: 'closed' }), [
    { cls: 'meta', text: 'session ended: closed' },
  ]);
  assert.deepEqual(eventLines({ type: 'session.ended' }), [{ cls: 'meta', text: 'session ended: closed' }]);
});

test('countDiff: the same numbers git diff --stat would print', () => {
  assert.deepEqual(countDiff('--- a\n+++ b\n+one\n-two\n+three\n'), { add: 2, del: 1 });
  assert.deepEqual(countDiff(''), { add: 0, del: 0 });
  assert.deepEqual(countDiff('  +not a diff line\n'), { add: 0, del: 0 });
});

test('firstLine: the first line with anything on it', () => {
  assert.equal(firstLine('\n\n  hello  \nworld'), 'hello');
  assert.equal(firstLine('   \n\t\n'), '');
  assert.equal(firstLine(''), '');
  assert.equal(firstLine(null), '');
});

test('todoLines: the marks are ASCII notation, and the state is kept', () => {
  assert.deepEqual(todoLines([{ state: 'done', text: 'look at app.js' }, { text: 'run the tests' }]), [
    { state: 'done', mark: '[x]', text: 'look at app.js' },
    { state: 'todo', mark: '[ ]', text: 'run the tests' },
  ]);
  assert.deepEqual(todoLines(null), []);
});
