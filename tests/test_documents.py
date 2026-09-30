"""Reading a PDF or a spreadsheet, into text.

A spreadsheet is a zip of XML and is read here, with the standard library,
because a feature that needs an install to open the file most people attach is
a feature half the people who want it will not turn on.

A PDF is a font-and-geometry format and is **not** — a hand-written
extractor gets the easy cases and garbles the rest, and a garbled document is
worse than a refusal because it reads as a bug and gets quoted from. So that
is an optional extra, and the refusal names the install rather than calling
the file binary.

The tests are mostly about the places a spreadsheet reader goes wrong,
because they are all silent when they do.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from openmirror.agent.documents import (
    DocumentError,
    _relationships,
    _resolve,
    is_document,
    read_document,
    read_xlsx,
)


def workbook(*, sheet_names=('People', 'Empty'), cells: str = '', shared: str = '') -> bytes:
    """A small but structurally real xlsx, written by hand.

    Real enough to catch the things that matter: a relationship table, a
    shared-string table, a string split across runs, and a cell with nothing
    in it.
    """
    names = ''.join(
        f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(sheet_names)
    )
    rels = ''.join(
        f'<Relationship Id="rId{i + 1}" Type="x" Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(sheet_names))
    )
    out = BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr(
            'xl/workbook.xml',
            '<?xml version="1.0"?><workbook '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{names}</sheets></workbook>',
        )
        z.writestr(
            'xl/_rels/workbook.xml.rels',
            '<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{rels}</Relationships>',
        )
        if shared:
            z.writestr(
                'xl/sharedStrings.xml',
                '<?xml version="1.0"?><sst '
                'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'{shared}</sst>',
            )
        for i in range(len(sheet_names)):
            z.writestr(
                f'xl/worksheets/sheet{i + 1}.xml',
                '<?xml version="1.0"?><worksheet '
                'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{cells if i == 0 else ""}</sheetData></worksheet>',
            )
    return out.getvalue()


SHEET = (
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
    '<row r="2"><c r="A2" t="inlineStr"><is><t>Bob</t></is></c>'
    '<c r="B2" t="n"><v>42</v></c><c r="C2" t="s"><v>2</v></c></row>'
)
SHARED = (
    '<si><t>Name</t></si>'
    '<si><t>Alice</t></si>'
    '<si><r><t>Senior eng</t></r><r><t>ineer</t></r></si>'
)


def test_a_sheet_is_read_with_its_own_name():
    """Not `sheet1`. The first version treated the relationship *id* as a path,
    so every lookup missed and every sheet came out named after its file — from
    a fallback that produced readable output, which is why it was not
    obviously broken. A workbook names its sheets; that is the content."""
    read = read_xlsx(workbook(cells=SHEET, shared=SHARED))
    assert read.sheets == ['People', 'Empty']
    assert '## People' in read.text and '## Empty' in read.text


def test_a_string_split_across_runs_is_joined():
    """Formatting splits a string into runs and the text is their
    concatenation. Reading only the first run loses most of every other
    cell."""
    read = read_xlsx(workbook(cells=SHEET, shared=SHARED))
    assert 'Senior engineer' in read.text
    assert 'Senior eng' in read.text and 'ineer' not in read.text.replace('Senior engineer', '')


def test_columns_are_placed_and_gaps_are_kept():
    """A spreadsheet is mostly empty cells, and a reader that drops them loses
    every column alignment — which is the entire content of a table."""
    read = read_xlsx(workbook(cells=SHEET, shared=SHARED))
    rows = [line for line in read.text.splitlines() if '\t' in line]
    assert rows[0] == 'Name\t\tAlice', rows[0]
    assert rows[1] == 'Bob\t42\tSenior engineer', rows[1]


def test_inline_strings_and_numbers():
    read = read_xlsx(workbook(cells=SHEET, shared=SHARED))
    assert 'Bob' in read.text and '42' in read.text


def test_a_workbook_with_no_shared_strings_still_reads():
    """Legal, and a reader that insists on the table refuses a real file."""
    cells = '<row r="1"><c r="A1" t="inlineStr"><is><t>only</t></is></c></row>'
    assert 'only' in read_xlsx(workbook(cells=cells)).text


def test_the_sheet_xml_escape_is_unwrapped():
    """A leading underscore is how a file stores a character that would
    otherwise be markup, and it arrives from other spreadsheets often enough
    to be worth taking out."""
    shared = '<si><t>a_x000D_b</t></si>'
    read = read_xlsx(workbook(cells='<row r="1"><c r="A1" t="s"><v>0</v></c></row>', shared=shared))
    assert 'a\rb' in read.text


def test_a_long_sheet_is_cut_and_says_so():
    cells = ''.join(f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>r{n}</t></is></c></row>' for n in range(1, 40))
    read = read_xlsx(workbook(cells=cells), max_rows=10)
    assert read.truncated is True
    assert 'r39' not in read.text


def test_a_file_that_is_not_a_spreadsheet_says_so(tmp_path):
    path = tmp_path / 'notes.xlsx'
    path.write_bytes(b'this is plain text')
    with pytest.raises(DocumentError, match='not a readable spreadsheet'):
        read_xlsx(path.read_bytes())


def test_the_relationship_table_is_actually_read():
    with zipfile.ZipFile(BytesIO(workbook())) as z:
        rels = _relationships(z)
        assert rels['rId1'] == 'worksheets/sheet1.xml'
        assert _resolve(z, 'rId1', rels) == 'xl/worksheets/sheet1.xml'


def test_which_files_are_documents():
    assert is_document(Path('a.pdf')) and is_document(Path('a.xlsx')) and is_document(Path('a.XLSM'))
    assert not is_document(Path('a.py')) and not is_document(Path('a.docx'))


# --- pdf ----------------------------------------------------------------------


def test_a_pdf_with_no_text_reader_says_which_install_fixes_it(monkeypatch):
    """Calling a PDF "binary" is a lie: the file is a document, this just
    cannot read it yet, and the difference is the difference between a bug
    report and an install command."""
    import builtins

    from openmirror.agent import documents

    real = builtins.__import__
    monkeypatch.setattr(
        builtins, '__import__',
        lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name)) if name == 'pypdf' else real(name, *a, **k),
    )
    with pytest.raises(DocumentError, match=r"openmirror\[docs\]"):
        documents.read_pdf(b'%PDF-1.7 anything')


def test_a_pdf_that_is_not_one_says_so():
    from openmirror.agent.documents import read_pdf

    with pytest.raises(DocumentError, match='could not be opened'):
        read_pdf(b'this is not a pdf at all')


def test_a_real_pdf_is_read_when_the_extra_is_there(tmp_path):
    pytest.importorskip('pypdf', reason='the [docs] extra is not installed')
    from pypdf import PdfWriter

    out = tmp_path / 'a.pdf'
    with open(out, 'wb') as handle:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(handle)

    read = read_document(out)
    assert read is not None
    assert read.kind == 'pdf' and read.pages == 1
    assert 'page 1' in read.text
    # And the reader says what it is not doing, because the text of a PDF is
    # glyph positions rather than paragraphs.
    assert 'reflowed' in read.note


def test_the_tool_reads_a_document_rather_than_calling_it_binary(tmp_path):
    """End to end, through `read_file` — because the whole point is that a PDF
    reaches the model as text instead of a refusal."""
    import asyncio

    from openmirror.agent.tools.base import ToolContext
    from openmirror.agent.tools.files import ReadTool

    data = workbook(cells=SHEET, shared=SHARED)
    (tmp_path / 's.xlsx').write_bytes(data)
    ctx = ToolContext(
        root=tmp_path, cwd=tmp_path, emit=None, ask=None, session_id='s', confined=True
    )
    out = asyncio.run(ReadTool().run({'path': 's.xlsx'}, ctx))
    assert '## People' in out.content
    assert out.display['kind'] == 'spreadsheet'
