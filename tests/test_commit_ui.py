"""The commit bar: it appears only in a repository, and it drafts rather than
commits.

Same split as `test_mail_ui.py`. The wiring is read as text, because "the
button is wired to the function that drafts" is a fact about the file; the
two decisions that can be checked as logic are checked as logic.

The properties, in order of how much they matter:

* **Nothing reaches a commit without a message somebody read.** The AI button
  fills a field. Committing reads that field. There is no code path from the
  draft to `POST /api/git/commit`, and the client-side one is guarded on the
  field being non-empty.
* **A commit does not quietly sweep up the rest of the project.** The commit
  call sends the message and nothing else; staging is a separate, explicit
  action, and "stage everything" is a button somebody presses.
* **A new project clears the old status.** The bar follows the session's
  working root, and showing the previous project's changed-file list for even
  one frame is how the wrong thing gets staged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'openmirror' / 'static'
COMMIT = (STATIC / 'commit.js').read_text()
APP = (STATIC / 'app.js').read_text()
INDEX = (STATIC / 'index.html').read_text()
GIT_ROUTER = (ROOT / 'openmirror' / 'routers' / 'git.py').read_text()


def node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


def run(script: str) -> dict:
    result = subprocess.run(
        [node(), '--input-type=module', '-e', script],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(f'node failed:\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])


# -- the bar is wired to the session, not to a picker -------------------------


def test_the_root_comes_from_the_session():
    """The question "commit this" is asked about the project you are already
    in, and asking which one on top of that is a question with one answer."""
    assert 'export function setCommitRoot' in COMMIT
    assert 'setCommitRoot(info.root);' in APP
    assert "import { setCommitRoot, wireCommit } from './commit.js';" in APP
    assert 'wireCommit();' in APP


def test_the_bar_is_hidden_with_no_root():
    assert 'if (!state.root) {' in COMMIT
    assert 'bar.hidden = true;' in COMMIT


def test_the_bar_counts_files_not_lines():
    """A diffstat of a reformat is large and says nothing about whether there
    is anything to commit. The number that answers the question is files."""
    assert 'changed(state.status)' in COMMIT
    assert '${count} changed' in COMMIT
    out = run('''
      const changed = (d) => !d ? 0 : (d.staged || []).length + (d.unstaged || []).length + (d.untracked || []).length;
      console.log(JSON.stringify({
        empty: changed({ staged: [], unstaged: [], untracked: [] }),
        some: changed({ staged: [{ path: 'a' }], unstaged: [{ path: 'b' }], untracked: [{ path: 'c' }] }),
        none: changed(null),
      }));
    ''')
    assert out == {'empty': 0, 'some': 3, 'none': 0}


def test_switching_projects_drops_the_previous_status():
    """Otherwise the list describes a different tree, and staging from it
    stages the wrong thing."""
    assert 'if (changedRoot) {' in COMMIT
    assert 'state.status = null;' in COMMIT
    assert 'refresh();' in COMMIT


# -- the two buttons -----------------------------------------------------------


def test_the_ai_button_drafts_and_the_commit_button_commits():
    assert 'id="commit-ai"' in INDEX and 'id="commit-go"' in INDEX
    assert 'ai.onclick = propose;' in COMMIT
    assert 'go.onclick = commit;' in COMMIT
    # Null-guarded, because a bar that is not on the page must not stop the
    # rest of the interface from wiring itself up.
    assert 'if (ai) ai.onclick = propose;' in COMMIT
    # The draft path cannot reach the commit endpoint.
    propose = COMMIT[COMMIT.index('async function propose'):COMMIT.index('async function commit')]
    assert '/api/git/propose' in propose
    assert '/api/git/commit' not in propose


def test_committing_with_an_empty_message_refuses_before_the_request():
    """A commit with no message is refused, not sent and refused by git: the
    guard is here so the person is told in the field they are looking at."""
    body = COMMIT[COMMIT.index('async function commit'):COMMIT.index('export function wireCommit')]
    assert 'if (!message) {' in body
    assert "a commit needs a message" in body
    assert body.index('if (!message) {') < body.index("/api/git/commit")


def test_a_commit_sends_the_message_and_nothing_else():
    """Staging is separate and explicit. A commit that swept up everything
    that changed is how an hour of unrelated work lands in one commit."""
    body = COMMIT[COMMIT.index('async function commit'):COMMIT.index('export function wireCommit')]
    assert "send('/api/git/commit', { root: state.root, message })" in body
    assert 'stage:' not in body


def test_the_draft_is_put_in_the_field_and_the_person_still_edits_it():
    assert "$('#commit-message').value = data.message;" in COMMIT
    assert 'read it, change what you want' in COMMIT
    # The field is not cleared by drafting, so an edit survives a second press.
    assert COMMIT[COMMIT.index('async function propose'):COMMIT.index('async function commit')].count(
        "$('#commit-message').value = ''"
    ) == 0


def test_the_button_says_it_does_not_commit():
    assert 'It does not commit' in INDEX
    assert 'a first draft' in COMMIT


# -- the server, as text --------------------------------------------------------


def test_the_server_also_keeps_the_two_apart():
    assert "@router.post('/propose')" in GIT_ROUTER
    assert "@router.post('/commit')" in GIT_ROUTER
    propose = GIT_ROUTER[GIT_ROUTER.index("@router.post('/propose')"):GIT_ROUTER.index("@router.post('/commit')")]
    assert 'core.commit(' not in propose, 'the draft endpoint must not be able to commit'
    assert 'MessageRequest' not in propose


def test_a_commit_takes_a_message_and_never_asks_for_one_to_be_generated():
    """There is no `draft: true` on the commit body, so there is no way to
    ask for a message and a commit in the same request."""
    assert 'class CommitRequest(BaseModel):' in GIT_ROUTER
    fields = GIT_ROUTER[GIT_ROUTER.index('class CommitRequest'):GIT_ROUTER.index('class ProposeRequest')]
    assert 'message: str' in fields
    for forbidden in ('ai', 'generate', 'propose', 'draft'):
        assert forbidden not in fields, forbidden


def test_nothing_staged_is_reported_with_gits_own_words():
    """Not a Python message: git says what it did not commit and names the
    file, and the fix is two buttons away."""
    assert "status_code=400" in GIT_ROUTER
    assert 'raise HTTPException(status_code=400, detail=str(exc)) from exc' in GIT_ROUTER


# -- the client helper, which is the bug this file's author hit ---------------


def test_a_post_that_wants_data_uses_send_and_not_post():
    """`post` returns a `Response` and `json` returns the body or null. Between
    them there is no way to write the thing these calls want: the success case
    needs the body, and the failure case needs the `detail` the server sent —
    and `json` throws the second away exactly when it is worth showing.

    Reading `data.message` off a `Response` is `undefined`, so the button did
    nothing and said nothing. Observed, not hypothesised.
    """
    dom = (STATIC / 'dom.js').read_text()
    assert 'export async function send(' in dom
    assert 'detail' in dom
    for module in (COMMIT, (STATIC / 'mail.js').read_text()):
        assert 'await post(' not in module, 'a post that is read as data'
        assert 'await send(' in module
        assert 'const { data, detail } = await send(' in module


def test_the_draft_buttons_are_bounded():
    """A button that says "writing…" for ever has no way out but reloading the
    page, and reloading loses whatever else was open in the dialog. Measured:
    a two-line diff on a reasoning model took over ninety seconds more than
    once."""
    for module in (COMMIT, (STATIC / 'mail.js').read_text()):
        assert 'timeout: 120_000' in module, 'a draft request with no bound'
    dom = (STATIC / 'dom.js').read_text()
    assert 'AbortSignal.timeout' in dom
    # And an abort is reported as a slow model, not as a daemon that has gone.
    assert 'may be slow or busy' in dom
    assert 'the daemon did not answer' in dom
