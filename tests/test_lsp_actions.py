"""Asking a language server to change files: code actions, rename, formatting.

The other half of `tests/test_lsp.py`. That file is about questions and the
answers that can be wrong silently — an empty answer, a busy server, a
diagnostic that never arrived. This one is about edits, where a client that is
wrong does not return nothing: it rewrites a file. So the fake server
(tests/fixtures/lsp_server.py) offers its fixes in the forms real servers use
and differ over — `documentChanges` with a range inside a line, `changes` over
whole lines, one spread across two files — and offers two of them aimed
outside the working root, so a client that applies what it is given writes a
file nobody asked it to.

Every question here is asked of the fake. Nothing needs a language server to
be installed, and nothing depends on one that happens to be: a real server's
formatter is a different program from its analyser, and a test that assumed
both would skip itself away on most machines and fail on the rest.

The arithmetic is checked separately from the protocol. `apply_text_edits` is
a pure function over a string, and the cases that matter — a column after an
emoji, an end position one line past the end of the file, two edits
overlapping — are cheaper and clearer to pin down there than through a
subprocess that has to agree about all of it first.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from openmirror.agent.approval import Mode
from openmirror.agent.lsp import (
    LspError,
    LspPool,
    ServerSpec,
    TextEdit,
    apply_edit,
    apply_text_edits,
    parse_workspace_edit,
)
from openmirror.agent.runtime import build_session
from openmirror.agent.tools.base import ToolContext, ToolError
from openmirror.agent.tools.lsp import LspTool
from openmirror.protocol.agent import Risk, ToolCompleted
from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
from tests.test_agent import ScriptedProvider, turn

FAKE = Path(__file__).parent / 'fixtures' / 'lsp_server.py'

# Two astral characters: one code point each, and two UTF-16 code units each,
# which is the entire difference between a position the protocol meant and the
# Python index a client would use for it. Everything below is about the two
# disagreeing, in both directions — the offset a server sends, and the column
# a client has to count to ask.
EMOJI = '\U0001F30D\U0001F30D'
# The symbol sits after the emoji on the same line, which is the direction the
# client counts: a client counting code points points the server at the space
# before the name, and the server finds no word there.
SOURCE = (
    f'LABEL = "{EMOJI}"; area = 1\n'
    '\n'
    '\n'
    'def other(r):\n'
    '    return area\n'
)
# `area_size` is here to be left alone: a rename is the edit with the most
# positions in it, and the ones next to them are as much the test as its own.
OTHER = 'import m\nprint(m.area, m.area_size)\n'


def fake() -> ServerSpec:
    return ServerSpec(
        name='fakels', command=[sys.executable, str(FAKE)], extensions=('.py',),
        language='Python', binary=sys.executable,
    )


def ctx(root: Path) -> ToolContext:
    async def emit(text, stream):
        pass

    async def ask(question, options, multi):
        return ''

    return ToolContext(root=root, cwd=root, emit=emit, ask=ask, session_id='lsp')


@pytest.fixture
async def tool(tmp_path: Path):
    """A tool over a project with one file, in the state the tests write."""
    (tmp_path / 'm.py').write_text(SOURCE)
    lsp = LspTool(LspPool(tmp_path, [fake()]))
    yield lsp
    await lsp.close()


# -- listing -----------------------------------------------------------------


async def test_the_fixes_on_offer_are_listed_with_their_kind_and_change_nothing(tool, tmp_path: Path):
    (tmp_path / 'bad.py').write_text('BROKEN = 1\n')
    out = await tool.run({'operation': 'code_action', 'path': 'bad.py', 'line': 1, 'symbol': 'BROKEN'}, ctx(tmp_path))

    assert '1 code action offered in bad.py' in out.content
    assert 'quickfix' in out.content and 'Replace BROKEN with fine' in out.content
    # The number, because the model is shown a list and then names one from it.
    assert 'Apply one with action=<number>' in out.content
    assert (tmp_path / 'bad.py').read_text() == 'BROKEN = 1\n'


async def test_two_fixes_on_one_line_are_both_listed(tool, tmp_path: Path):
    (tmp_path / 'two.py').write_text('BROKEN = 1  # TODO: why\nx = 2\n')
    out = await tool.run({'operation': 'code_action', 'path': 'two.py', 'line': 1, 'symbol': 'BROKEN'}, ctx(tmp_path))

    assert '2 code actions offered' in out.content
    assert 'Replace BROKEN with fine' in out.content
    assert 'Delete the TODO line' in out.content
    assert 'changes 1 file' in out.content


async def test_both_forms_of_a_workspace_edit_are_read(tmp_path: Path):
    """`changes` is a map of uri to edits; `documentChanges` is a list, and the
    only form that can carry file operations. A client that reads one of them
    and ignores the other applies half of what it was sent, so both are asked
    for here and checked by which shape came back."""
    (tmp_path / 'm.py').write_text('BROKEN = 1\n# TODO: later\n')
    uri = (tmp_path / 'm.py').as_uri()
    pool = LspPool(tmp_path, [fake()])
    try:
        server = await pool.server(pool.spec_for('m.py'))
        await server.settle(20)
        await server.sync(tmp_path / 'm.py')

        quick = (await server.code_action(tmp_path / 'm.py', {'line': 0, 'character': 0}))[0]
        assert quick.edit.document_changes is True
        assert quick.edit.raw['documentChanges'][0]['edits'][0]['newText'] == 'fine'
        assert quick.edit.files[0].uri == uri
        assert quick.edit.files[0].edits == [TextEdit(0, 0, 0, 6, 'fine')]

        slow = (await server.code_action(tmp_path / 'm.py', {'line': 1, 'character': 0}))[0]
        assert slow.edit.document_changes is False
        assert slow.edit.file_ops == []
        whole_line = {'start': {'line': 1, 'character': 0}, 'end': {'line': 2, 'character': 0}}
        assert slow.edit.raw['changes'] == {uri: [{'range': whole_line, 'newText': ''}]}
    finally:
        await pool.close()


async def test_a_range_with_nothing_wrong_in_it_says_so_rather_than_nothing_at_all(tool, tmp_path: Path):
    out = await tool.run({'operation': 'code_action', 'path': 'm.py', 'line': 5, 'symbol': 'return'}, ctx(tmp_path))
    assert out.content == 'No code actions offered in m.py.'


# -- applying ----------------------------------------------------------------


async def test_choosing_a_fix_writes_it_and_shows_the_difference(tool, tmp_path: Path):
    """The `documentChanges` form, with a range inside a line, on a line whose
    columns are not code points. The whole point is the emoji: `LABEL = "\U0001F30D\U0001F30D"; area = 1`."""
    (tmp_path / 'emoji.py').write_text(f'LABEL = "{EMOJI}"; BROKEN = 1\n')
    args = {'operation': 'code_action', 'path': 'emoji.py', 'line': 1, 'symbol': 'BROKEN'}

    listed = await tool.run(args, ctx(tmp_path))
    assert '1 code action offered' in listed.content

    applied = await tool.run({**args, 'action': '0'}, ctx(tmp_path))
    assert applied.display['path'] == str(tmp_path / 'emoji.py')
    assert (tmp_path / 'emoji.py').read_text() == f'LABEL = "{EMOJI}"; fine = 1\n'
    assert f'-LABEL = "{EMOJI}"; BROKEN = 1' in applied.display['diff']
    assert f'+LABEL = "{EMOJI}"; fine = 1' in applied.display['diff']


async def test_a_fix_can_be_named_by_part_of_its_title(tool, tmp_path: Path):
    """A model asked to choose a fix paraphrases its title, so a partial title
    is matched — and a refusal that says what the options were is one it can
    recover from."""
    (tmp_path / 'todo.py').write_text('keep = 1\n# TODO: later\nkeep = 2\n')
    picked = await tool.run(
        {'operation': 'code_action', 'path': 'todo.py', 'line': 2, 'symbol': 'TODO', 'action': 'todo line'},
        ctx(tmp_path),
    )
    assert (tmp_path / 'todo.py').read_text() == 'keep = 1\nkeep = 2\n'
    assert 'Delete the TODO line' in picked.content

    (tmp_path / 'bad.py').write_text('BROKEN = 1\n')
    with pytest.raises(ToolError, match="no action here matches 'delete the todo'"):
        await tool.run(
            {'operation': 'code_action', 'path': 'bad.py', 'line': 1, 'symbol': 'BROKEN', 'action': 'delete the todo'},
            ctx(tmp_path),
        )
    assert (tmp_path / 'bad.py').read_text() == 'BROKEN = 1\n'


async def test_a_number_that_is_not_on_the_list_says_what_to_do(tool, tmp_path: Path):
    (tmp_path / 'bad.py').write_text('BROKEN = 1\n')
    with pytest.raises(ToolError, match='there is no action number 3'):
        await tool.run(
            {'operation': 'code_action', 'path': 'bad.py', 'line': 1, 'symbol': 'BROKEN', 'action': '3'},
            ctx(tmp_path),
        )


# -- rename ------------------------------------------------------------------


async def test_rename_changes_every_file_the_symbol_is_in(tool, tmp_path: Path):
    (tmp_path / 'n.py').write_text(OTHER)
    out = await tool.run(
        {'operation': 'rename', 'path': 'm.py', 'line': 1, 'symbol': 'area', 'new_name': 'surface'}, ctx(tmp_path),
    )

    # One edit per occurrence, in two files, several of them after an emoji on
    # the same line, and one of them a name that merely starts with the same
    # letters — which the server did not send an edit for, and so must survive.
    assert (tmp_path / 'm.py').read_text() == SOURCE.replace('area', 'surface')
    assert (tmp_path / 'n.py').read_text() == 'import m\nprint(m.surface, m.area_size)\n'
    assert out.content.startswith("renamed area to 'surface' — 2 files:")
    assert 'm.py  (2 changes)' in out.content and 'n.py  (1 change)' in out.content


async def test_rename_finds_the_name_after_an_emoji(tool, tmp_path: Path):
    """The other direction of the same arithmetic: the client counts the
    column to send. Counting code points would point at the space before it,
    the server would find no word there, and the rename would come back
    refusing rather than wrong — which is the failure worth naming."""
    (tmp_path / 'p.py').write_text(f'def f():\n    x = 1  # {EMOJI} area of the disc\n')
    out = await tool.run(
        {'operation': 'rename', 'path': 'p.py', 'line': 2, 'symbol': 'area', 'new_name': 'region'}, ctx(tmp_path),
    )
    assert (tmp_path / 'p.py').read_text() == f'def f():\n    x = 1  # {EMOJI} region of the disc\n'
    assert out.display['path'] == str(tmp_path / 'p.py')


# -- formatting --------------------------------------------------------------


async def test_formatting_rewrites_the_file_and_says_when_there_is_nothing_to_do(tool, tmp_path: Path):
    (tmp_path / 'ugly.py').write_text('a = 1   \nb = 2  \n')
    out = await tool.run({'operation': 'format', 'path': 'ugly.py'}, ctx(tmp_path))

    # No trailing spaces, and the final newline the file did not have — the
    # edit ends one line past the last, which is how the protocol says "to the
    # end of the file" and which a client has to clamp rather than take
    # literally.
    assert (tmp_path / 'ugly.py').read_text() == 'a = 1\nb = 2\n'
    assert out.content == 'formatted ugly.py — 1 file:\nugly.py  (1 change)'

    again = await tool.run({'operation': 'format', 'path': 'ugly.py'}, ctx(tmp_path))
    assert again.content.startswith('fakels had nothing to format in ugly.py — it is already formatted')
    assert (tmp_path / 'ugly.py').read_text() == 'a = 1\nb = 2\n'


async def test_a_range_is_formatted_and_the_rest_of_the_file_is_left_alone(tool, tmp_path: Path):
    (tmp_path / 'ugly.py').write_text('a = 1   \nb = 2  \nc = 3  \n')
    out = await tool.run(
        {'operation': 'format', 'path': 'ugly.py', 'line': 1, 'end_line': 2}, ctx(tmp_path),
    )
    assert (tmp_path / 'ugly.py').read_text() == 'a = 1\nb = 2\nc = 3  \n'
    assert 'formatted ugly.py:1-2' in out.content


async def test_a_server_that_cannot_format_a_range_says_what_to_do_instead(tool, tmp_path: Path):
    """The fake declares both, so this is the one case where the client asks
    for a capability and the answer decides. Nothing is wrong with the file;
    the point is the refusal naming the alternative."""
    (tmp_path / 'ugly.py').write_text('a = 1   \nb = 2  \n')
    lsp = LspTool(LspPool(tmp_path, [fake()]))
    try:
        server = await lsp.pool.server(lsp.pool.spec_for('ugly.py'))
        server.capabilities.pop('documentRangeFormattingProvider')
        with pytest.raises(ToolError, match='does not answer format questions. Run the project'):
            await lsp.run({'operation': 'format', 'path': 'ugly.py', 'line': 1, 'end_line': 2}, ctx(tmp_path))
        # The whole file is still its business, and was left alone.
        assert (tmp_path / 'ugly.py').read_text() == 'a = 1   \nb = 2  \n'
        assert (await lsp.run({'operation': 'format', 'path': 'ugly.py'}, ctx(tmp_path))).content.startswith('formatted')
    finally:
        await lsp.close()


# -- confinement -------------------------------------------------------------


async def test_an_edit_reaching_outside_the_root_is_refused_and_nothing_is_written(tool, tmp_path: Path):
    outside = tmp_path.parent / 'escaped.py'
    outside.write_text('untouched = True\n')
    (tmp_path / 'bad.py').write_text('keep = 1\nESCAPE = 2\n')

    with pytest.raises(ToolError, match='reaches outside the working root'):
        await tool.run(
            {'operation': 'code_action', 'path': 'bad.py', 'line': 2, 'symbol': 'ESCAPE', 'action': '0'}, ctx(tmp_path),
        )

    # Not the file it wanted to write, and not the one in the project either —
    # the same edit carried both, and half of it is the hard one to notice.
    assert outside.read_text() == 'untouched = True\n'
    assert (tmp_path / 'bad.py').read_text() == 'keep = 1\nESCAPE = 2\n'


async def test_a_symlink_out_of_the_root_is_refused_too(tool, tmp_path: Path):
    """A textual check on the path passes this one: the URI is inside the root
    and the file it names is not. Resolving first and comparing after is the
    only ordering that catches it."""
    outside = tmp_path.parent / 'behind-the-link.py'
    outside.write_text('untouched = True\n')
    link = tmp_path / 'link-out.py'
    link.symlink_to(outside)
    (tmp_path / 'bad.py').write_text('keep = 1\nSYMLINK = 2\n')

    with pytest.raises(ToolError, match='reaches outside the working root'):
        await tool.run(
            {'operation': 'code_action', 'path': 'bad.py', 'line': 2, 'symbol': 'SYMLINK', 'action': '0'}, ctx(tmp_path),
        )
    assert outside.read_text() == 'untouched = True\n'
    assert (tmp_path / 'bad.py').read_text() == 'keep = 1\nSYMLINK = 2\n'


async def test_an_edit_is_refused_whole_when_one_path_is_outside(tool, tmp_path: Path):
    (tmp_path / 'm.py').write_text('keep = 1\n')
    inside = (tmp_path / 'm.py').as_uri()
    edit = parse_workspace_edit({'documentChanges': [
        {'textDocument': {'uri': inside}, 'edits': [
            {'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 4}}, 'newText': 'gone'}]},
        {'textDocument': {'uri': 'file:///elsewhere/x.py'}, 'edits': [
            {'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}, 'newText': 'y'}]},
    ]})

    def resolve(uri: str) -> Path:
        if uri == inside:
            return tmp_path / 'm.py'
        raise ToolError(f'{uri}: outside the session root')

    with pytest.raises(LspError, match='none of it was written'):
        await apply_edit(edit, resolve)
    assert (tmp_path / 'm.py').read_text() == 'keep = 1\n'


async def test_a_server_does_not_get_a_side_door_through_apply_edit(tool, tmp_path: Path):
    """The fixture asks the client to write a file on its behalf as it starts,
    and the client's answer is the one it has always given."""
    assert apply_edit_answer() == {'applied': False, 'failureReason': 'this client does not apply edits'}

    await tool.run({'operation': 'symbols', 'path': 'm.py'}, ctx(tmp_path))
    assert not (tmp_path / 'side-door.py').exists()


def apply_edit_answer() -> dict:
    """What this client answers the server's own workspace/applyEdit with."""
    from openmirror.agent.lsp import LanguageServer

    return LanguageServer(fake(), Path('.'))._answer('workspace/applyEdit', {'edit': {}})


# -- the arithmetic ----------------------------------------------------------


def chars(text: str) -> int:
    """A length the way the protocol counts one, so a test can say where a
    server's character offset is without counting by hand."""
    return len(text.encode('utf-16-le')) // 2


def test_an_offset_after_an_emoji_is_counted_in_code_units():
    prefix = f'x = 1; y = "{EMOJI}"; '
    line = f'{prefix}BROKEN = 1'
    out = apply_text_edits(line, [TextEdit(0, chars(prefix), 0, chars(prefix) + 6, 'fine')])
    assert out == f'{prefix}fine = 1'


def test_an_offset_that_lands_inside_a_character_keeps_the_character():
    one = '\U0001F30D'  # one code point, two UTF-16 code units
    line = f'a{one}{one}b'
    # Character 2 is between the two halves of the first emoji, which is a
    # server's bug rather than a client's. Clamped to the boundary of the
    # character it belongs to, and the character is not split in half.
    assert apply_text_edits(line, [TextEdit(0, 2, 0, 2, 'X')]) == f'a{one}X{one}b'
    assert apply_text_edits(line, [TextEdit(0, 5, 0, 5, 'X')]) == f'{line[:-1]}Xb'
    assert len(apply_text_edits(line, [TextEdit(0, 2, 0, 2, 'X')])) == len(line) + 1


def test_a_position_past_the_end_of_the_file_clamps_to_its_end():
    # A whole line, which is how a deletion is written: the end of one line is
    # the start of the next.
    assert apply_text_edits('a = 1\nb = 2\n', [TextEdit(0, 0, 1, 0, '')]) == 'b = 2\n'
    # The whole file, ending on the line after the last. A file that does not
    # end in a newline has no such line, and one that names a line which is not
    # there is clamped rather than refused.
    assert apply_text_edits('one\ntwo', [TextEdit(0, 0, 2, 0, '')]) == ''
    assert apply_text_edits('one\ntwo', [TextEdit(0, 0, 400, 0, '')]) == ''
    # A character past the end of a line is the end of that line, and one
    # before the start of the document is its start.
    assert apply_text_edits('one\ntwo', [TextEdit(0, 0, 0, 400, 'X')]) == 'Xtwo'
    assert apply_text_edits('one\ntwo', [TextEdit(0, -3, 0, 0, 'X')]) == 'Xone\ntwo'


def test_edits_are_applied_from_the_last_one_backwards():
    # Two on one line, in the order a server would list them. Applied forwards,
    # the first would move the second's offsets and leave `ell-llipse`.
    out = apply_text_edits('apple ellipse\n', [
        TextEdit(0, 0, 0, 5, 'APPLE'),
        TextEdit(0, 6, 0, 13, 'ELLIPSE'),
    ])
    assert out == 'APPLE ELLIPSE\n'


def test_overlapping_edits_are_refused_rather_than_guessed_at():
    with pytest.raises(LspError, match='overlap'):
        apply_text_edits('apple\n', [TextEdit(0, 0, 0, 5, 'X'), TextEdit(0, 3, 0, 8, 'Y')])


def to(path: Path):
    """A resolver that allows this one file and nothing else to be written."""
    return lambda uri: path


def test_an_edit_that_changes_nothing_does_not_touch_the_file(tmp_path: Path):
    path = tmp_path / 'same.py'
    path.write_text('a = 1\n')
    written = asyncio.run(apply_edit(
        parse_workspace_edit({'changes': {path.as_uri(): [
            {'range': {'start': {'line': 0, 'character': 1}, 'end': {'line': 0, 'character': 2}}, 'newText': ' '}]}}),
        to(path),
    ))
    assert written[0].edits == 0
    assert path.read_text() == 'a = 1\n'


def test_a_file_this_client_cannot_decode_is_not_rewritten(tmp_path: Path):
    """Rewriting a file it could not read would replace every byte it did not
    understand with U+FFFD, which is a corruption dressed as a fix."""
    path = tmp_path / 'latin1.py'
    path.write_bytes(b'a = "\xe9"\n')
    with pytest.raises(LspError, match='not text this client can rewrite'):
        asyncio.run(apply_edit(
            parse_workspace_edit({'changes': {path.as_uri(): [
                {'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}, 'newText': 'b'}]}}),
            to(path),
        ))
    assert path.read_bytes() == b'a = "\xe9"\n'


def test_a_file_the_server_wants_to_delete_is_refused_outright(tmp_path: Path):
    """`documentChanges` can carry file operations as well as edits, and a
    client that quietly ignored them would report success for half an edit."""
    with pytest.raises(LspError, match='does not do'):
        asyncio.run(apply_edit(parse_workspace_edit({'documentChanges': [
            {'kind': 'delete', 'uri': (tmp_path / 'gone.py').as_uri()}]}), to(tmp_path / 'gone.py')))
    assert not (tmp_path / 'gone.py').exists()


# -- grading -----------------------------------------------------------------


async def test_listing_a_fix_is_a_read_and_the_three_writes_are_writes(tool, tmp_path: Path):
    c = ctx(tmp_path)
    listing = {'operation': 'code_action', 'path': 'm.py', 'line': 1, 'symbol': 'area'}
    assert tool.assess(listing, c).risk is Risk.EXECUTE  # nothing is running yet

    await tool.run(listing, c)  # starts the server
    assert tool.assess(listing, c).risk is Risk.READ
    assert tool.assess({**listing, 'action': '0'}, c).risk is Risk.WRITE
    assert tool.assess(
        {'operation': 'rename', 'path': 'm.py', 'line': 1, 'symbol': 'area', 'new_name': 'b'}, c,
    ).risk is Risk.WRITE
    assert tool.assess({'operation': 'format', 'path': 'm.py'}, c).risk is Risk.WRITE


async def test_a_call_that_starts_a_server_and_writes_is_graded_as_the_starting(tool, tmp_path: Path):
    """EXECUTE, not WRITE. The two are not equal — auto_edit runs file writes
    and asks about commands — so grading a call that does both as a write
    would make the one that also starts something the safer of the two to run
    unattended."""
    first = tool.assess({'operation': 'format', 'path': 'm.py'}, ctx(tmp_path))
    assert first.risk is Risk.EXECUTE
    assert 'start fakels' in first.summary and 'changes files' in first.summary


def test_a_call_that_cannot_run_is_refused_before_anything_is_asked(tool, tmp_path: Path):
    assert tool.assess({'operation': 'rename', 'path': 'm.py', 'line': 1, 'symbol': 'area'}, ctx(tmp_path)).invalid \
        == 'rename needs a new_name'
    assert 'end_line needs a line' in tool.assess(
        {'operation': 'format', 'path': 'm.py', 'end_line': 3}, ctx(tmp_path),
    ).invalid
    assert 'code_action' in tool.assess({'operation': 'code_action', 'path': 'm.py'}, ctx(tmp_path)).invalid


async def test_a_write_through_the_server_is_a_write_like_any_other(tmp_path: Path):
    """Graded, and heard from afterwards.

    The after-write hook that tells a model what its edit just broke fires for
    a server's edit too, because the write is made by a tool the policy has
    already graded rather than by the server — which is the whole difference
    between this and workspace/applyEdit.
    """
    (tmp_path / 'm.py').write_text('BROKEN = 1   \n')
    provider = ScriptedProvider([
        [StreamToolUse(id='c1', name='lsp', input={'operation': 'symbols', 'path': 'm.py'}), StreamDone(stop_reason='tool_use')],
        [StreamToolUse(id='c2', name='lsp', input={'operation': 'format', 'path': 'm.py'}), StreamDone(stop_reason='tool_use')],
        [StreamText(text='Formatted.'), StreamDone()],
    ])
    session = build_session(root=tmp_path, provider=provider, model='x', mode=Mode.TRUSTED, lsp=[fake()])
    await session.start()
    try:
        events = await turn(session, 'format it')
    finally:
        await session.close()

    assert (tmp_path / 'm.py').read_text() == 'BROKEN = 1\n'
    done = [e for e in events if isinstance(e, ToolCompleted) and e.result.name == 'lsp'][-1]
    assert done.result.ok and done.result.content.startswith('formatted m.py')
    assert 'fakels now reports 1 error in this file' in done.result.content


async def test_plan_mode_refuses_a_rename_and_leaves_the_file_alone(tmp_path: Path):
    (tmp_path / 'm.py').write_text(SOURCE)
    provider = ScriptedProvider([
        [StreamToolUse(id='c1', name='lsp', input={
            'operation': 'rename', 'path': 'm.py', 'line': 1, 'symbol': 'area', 'new_name': 'surface'}),
         StreamDone(stop_reason='tool_use')],
        [StreamText(text='I would rename it to surface.'), StreamDone()],
    ])
    session = build_session(root=tmp_path, provider=provider, model='x', mode=Mode.PLAN, lsp=[fake()])
    await session.start()
    try:
        events = await turn(session, 'rename it')
    finally:
        await session.close()

    assert (tmp_path / 'm.py').read_text() == SOURCE
    # The refusal is the policy's, not the tool's: nothing was asked of anyone,
    # because a plan is a thing to approve before the first edit.
    assert not [e for e in events if isinstance(e, ToolCompleted)]
    assert not session.tools['lsp'].pool._servers
