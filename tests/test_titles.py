"""A session named after its first sentence.

Claude Code titles a conversation from what was said in it. openmirror named it
after the directory, so a sidebar of a busy afternoon reads `openmirror`,
`openmirror`, `openmirror` — a list you have to open every row of to use, and
the one you want is in the middle of it.

These tests are mostly about what the title must *not* be, because that is where
this goes wrong. A cut in the middle of a word reads as a bug in the interface
rather than as brevity. An ellipsis promises an elision the sidebar has no room
to expand. A pasted URL is a title nobody can scan. A pasted code block titled
` ``` ` is worse than the directory name it replaced. And a mangled non-ASCII
title — one where a byte-boundary cut split a code point in half — is visible
in the first three characters, so it is checked explicitly.

The second half is about `needs_title`, and the rule it encodes is the
important one: a title anybody chose is never re-derived. Overwriting a user's
label is not a cosmetic bug, it is deleting something they cannot get back.
"""

from __future__ import annotations

import pytest

from openmirror.titles import needs_title, title_for

# --- the ordinary cases --------------------------------------------------------


def test_the_first_sentence_is_the_title():
    """Not the whole message: a title is a label, and the detail is one click
    away. Taking the whole first message produces a title nobody can scan."""
    got = title_for('Add a retry to the uploader. It should back off after two failures.')
    assert got == 'Add a retry to the uploader'


def test_a_message_that_does_not_end_in_a_full_stop_still_works():
    """Most first messages are a fragment. Insisting on a terminator would
    return the fallback for half of them."""
    assert title_for('add a retry to the uploader') == 'add a retry to the uploader'


def test_whitespace_is_collapsed():
    """A label has to be one line, and a transcript is not one."""
    assert title_for('  add   a\tretry\nto the uploader  ') == 'add a retry to the uploader'


def test_a_hard_wrapped_paragraph_is_joined_rather_than_cut():
    """A message pasted out of a terminal arrives as one sentence over three
    lines. Stopping at the newline titles the session with a third of it."""
    assert title_for('add a retry to the\nuploader and back it\noff twice') == (
        'add a retry to the uploader and back it off twice'
    )


def test_a_greeting_is_not_a_title():
    """"Sure." is a sentence and says nothing about the work. The first few
    sentences are taken until there is enough to name the turn, and then no
    more — a title built from three sentences is an opening line."""
    assert title_for('Sure. Add a retry to the uploader.') == 'Sure. Add a retry to the uploader'
    assert title_for('Hi. Add a retry to the uploader.') == 'Hi. Add a retry to the uploader'


def test_only_the_first_clause_of_a_long_one():
    """A semicolon and a spaced dash divide two complete thoughts, and the
    first is the one that names the session. A colon is not cut: "Note" and
    "Summary" on their own name nothing."""
    assert title_for('The bug; the parser drops empty files and the test never sees it') == 'The bug'
    assert title_for('Rename the store — it is not a cache') == 'Rename the store'
    assert title_for('Summary: everything is fine now') == 'Summary: everything is fine now'


def test_a_tiny_clause_is_not_worth_keeping():
    """The floor is what stops "Yes; on it" becoming a title called "Yes"."""
    assert title_for('Yes; on it') == 'Yes; on it'


# --- markdown, quotes and the wrappers a paste leaves behind -------------------


def test_markdown_is_stripped_rather_than_shown():
    """A pasted message arrives wearing its formatting, and `**Fix the parser**`
    is not what a sidebar should say."""
    assert title_for('**Bold** and *ital* and ~~struck~~ and `code`') == 'Bold and ital and struck and code'


def test_a_link_title_is_its_label_not_its_url():
    """The commonest paste in an issue tracker. The label is the title and the
    URL is where it lives."""
    got = title_for('See [Fix the parser](https://github.com/x/y/issues/1) for the details. It is reproducible.')
    assert got == 'See Fix the parser for the details'
    assert 'http' not in got


def test_an_asterisk_that_is_arithmetic_is_left_alone():
    """Deleting the character rather than the marker turns `2 * 3` into `2 3`,
    which is a different statement."""
    assert title_for('Use `2 * 3` to scale') == 'Use 2 * 3 to scale'
    assert title_for('2 * 3 = 6') == '2 * 3 = 6'


def test_quotes_and_trailing_punctuation_go():
    assert title_for('"fix the parser."') == 'fix the parser'
    assert title_for("'fix the parser'") == 'fix the parser'
    assert title_for('“fix the parser”') == 'fix the parser'
    assert title_for('6 inches, give or take') == '6 inches, give or take'


def test_a_balanced_bracket_is_part_of_the_title():
    """Only an *unbalanced* one is punctuation left behind by a cut. Dropping
    a matched pair would turn "Fix the parser (in parser.py)" into a lie."""
    assert title_for('Fix the parser (in parser.py)') == 'Fix the parser (in parser.py)'


def test_blockquotes_headings_and_bullets_are_stripped():
    assert title_for('> ## - Fix the parser') == 'Fix the parser'
    assert title_for('1. Fix the parser') == 'Fix the parser'


def test_a_leading_dash_that_is_arithmetic_is_left_alone():
    """A bullet needs its space. Without that rule "-1 does not parse" is
    titled "1 does not parse", which is a different bug report."""
    assert title_for('-1 does not parse, apparently') == '-1 does not parse, apparently'


# --- a first message with no words in it --------------------------------------


def test_an_empty_message_falls_back():
    """A session opened with the `/clear` key or an empty composer has no
    title in it. The fallback is the caller's placeholder, so it is nameless
    rather than blank."""
    assert title_for('', fallback='proj') == 'proj'
    assert title_for('   \n\t  \n', fallback='proj') == 'proj'
    assert title_for(None, fallback='proj') == 'proj'  # a message that is not a string


def test_a_bare_slash_command_falls_back():
    """`/compact` says what to do, not what the conversation is about. A session
    called "compact" is worse than one called after its directory."""
    assert title_for('/compact', fallback='proj') == 'proj'
    assert title_for('/compact') == ''


def test_a_slash_command_with_prose_keeps_the_prose():
    """The command is the wrapper; what follows it is the request."""
    assert title_for('/compact the long plan we discussed') == 'the long plan we discussed'


def test_the_line_after_a_bare_command_is_still_the_first_message():
    """`/compact` on the first line of a paste is a preamble, not a title."""
    assert title_for('/compact\nthe parser is dropping empty files') == 'the parser is dropping empty files'


def test_a_pasted_code_block_is_titled_by_its_first_line_of_code():
    """Anything beats "```", and the first line of a stack trace is at least
    recognisable."""
    assert title_for('```python\nValueError: bad token\n```') == 'ValueError: bad token'


def test_a_message_that_is_only_a_path_is_named_by_its_last_segment():
    """A path is not a sentence and a title of
    `src/openmirror/agent/session.py` is not scannable in a list of forty."""
    assert title_for('/home/you/proj/src/parser.py', fallback='proj') == 'parser.py'
    assert title_for('C:\\Users\\you\\parser.py', fallback='proj') == 'parser.py'


def test_a_message_that_is_only_a_url_is_named_by_its_site():
    """The tail of a URL is a path *into* a site, and the site is the
    identifiable half."""
    assert title_for('https://github.com/openai/openai-python/issues/42', fallback='proj') == 'github.com'
    assert title_for('https://docs.python.org/3/', fallback='proj') == 'docs.python.org'


def test_a_location_still_falls_back_when_it_names_nothing():
    """A URL with no host is not a URL, and the caller always knows better
    than a regex does."""
    assert title_for('///', fallback='proj') == 'proj'
    assert title_for('/home/you/proj/src/parser.py') == 'parser.py'


# --- the cut -------------------------------------------------------------------


def test_a_long_first_line_is_cut_on_a_word_boundary():
    """Mid-word reads as a bug in the interface rather than as brevity, and an
    ellipsis promises text the sidebar has no room to expand."""
    got = title_for('please make the session index resilient to a half written line at the end of a turn')
    assert got == 'please make the session index resilient to a half written'
    assert len(got) <= 60
    assert not got.endswith(' ')
    assert '…' not in got and '...' not in got


def test_the_cut_never_exceeds_the_budget():
    for message in ('a' * 500, 'word ' * 200, 'supercalifragilisticexpialidocious and then some more words here'):
        assert len(title_for(message)) <= 60


def test_a_word_longer_than_the_budget_is_cut_hard():
    """A word boundary that does not exist cannot be respected, and a broken
    one is worse than a short word."""
    assert title_for('x' * 200) == 'x' * 60
    assert len(title_for('a' * 90, max_chars=20)) == 20


def test_max_chars_is_respected_wherever_it_is_set():
    assert title_for('add a retry to the uploader and back off twice', max_chars=20) == 'add a retry to the'
    assert title_for('anything', max_chars=0) == 'a'


def test_non_ascii_survives_intact():
    """The cut is on characters, not bytes, so it cannot split a code point —
    and nothing here normalises, transliterates or lowercases, because a title
    a person typed in their own script is the one they want to read."""
    got = title_for('请把会话索引改成可以容忍最后一行被截断的情况。')
    assert got == '请把会话索引改成可以容忍最后一行被截断的情况'
    got = title_for('请把会话索引改成可以容忍最后一行被截断的情况，这样重启之后也不会丢掉之前的对话记录。')
    assert got.startswith('请把会话索引')
    assert got.endswith('记录')
    assert len(got) <= 60
    # Non-Latin punctuation is sentence-ending too, and is stripped with it.
    assert title_for('日本語のタイトル。sync') == '日本語のタイトル'
    # No transliteration and no case folding: the accent is the person's own
    # spelling. The dash does not cut here because "Café" is under the clause
    # floor, which is what keeps "Yes" from becoming a title.
    assert title_for('Café — a session about Unicode, please. And more.') == 'Café — a session about Unicode, please'


def test_emoji_are_not_mangled():
    """Four bytes each, and a byte-boundary cut turns half of one into a
    replacement character. Checked because it is invisible until it is not."""
    got = title_for('ship it 🚀🚀🚀 and then tell me what broke in the release notes for the launch')
    assert '🚀' in got
    assert '�' not in got


# --- whether there is a title yet ----------------------------------------------


def test_an_empty_title_is_always_a_placeholder():
    assert needs_title('', 'proj')
    assert needs_title('   ', 'proj')
    assert needs_title('', '')


def test_the_directory_name_is_a_placeholder_because_it_is_one():
    """`SessionManager.create` titles a session `Path(root).name` when the
    caller passed none. That is the placeholder, and recognising it is the
    only way a generated title can ever replace one."""
    assert needs_title('proj', 'proj')
    assert needs_title('proj', '/tmp/proj')


def test_a_title_anybody_set_is_never_re_derived():
    """The whole point. A user's label is the only name they have for a
    session and a generator that overwrites one is deleting something they
    cannot get back — quietly, which is what makes it unrecoverable."""
    assert not needs_title('Parser work', 'proj')
    assert not needs_title('Parser work', '')
    assert not needs_title('proj (fork)', 'proj')


def test_comparison_survives_the_spelling_of_the_placeholder():
    """A title is displayed normalised, so a comparison that missed on case or
    spacing would overwrite a real one. Both spellings of the placeholder are
    accepted because both occur at the call sites."""
    assert needs_title('PROJ', 'proj')
    assert needs_title('proj', 'Proj')
    assert not needs_title('Parser', '/tmp/proj')


def test_with_no_default_there_is_nothing_to_be_a_placeholder_against():
    """An empty default must not make every non-empty title a placeholder —
    that would overwrite the first title anybody ever set."""
    assert not needs_title('anything at all', '')


# --- the two together ----------------------------------------------------------


@pytest.mark.parametrize(
    'message',
    [
        'Fix the parser so it stops dropping empty files. Then bump the version.',
        '```\nbig stack trace\n```',
        '/compact',
        'https://example.com/a/b/c',
        '',
        '   ',
        '你好，世界。这是一段中文的测试消息，用来确认标题不会被破坏。',
    ],
)
def test_nothing_raises_and_everything_is_short_enough(message):
    """One sweep over the shapes above. The contract is a total function: a
    title generator that raises on a surprising first message takes the first
    turn of a session with it, and a title that is too long is a title the
    sidebar has to truncate itself."""
    for fallback in ('', 'proj'):
        got = title_for(message, fallback=fallback)
        assert isinstance(got, str)
        assert len(got) <= 60
        assert got == got.strip()
