"""Reading a PDF or a spreadsheet, into text.

Two formats and two very different amounts of work.

**A spreadsheet is a zip of XML.** `zipfile` and `xml.etree` are in the
standard library, and the format is small: a workbook is a list of sheets, each
sheet is cells, and the text is in `sharedStrings.xml` or inline in the cell.
So that is done here, in about a hundred lines, with no dependency — which
matters because xlsx is what most "here is the data" attachments are, and a
feature that needs an install to read one is a feature half the people who
want it will not turn on.

**A PDF is not.** It is a layout-and-typography format: fonts with custom
encodings, text positioned at coordinates, compression, object graphs that
reference each other. A hand-written extractor gets the easy cases and
garbles the rest, and a garbled document is worse than a refusal — it reads as
a bug and is quoted from.

So a PDF is read with `pypdf`, as an **optional extra**:

    pip install '.[docs]'

and when it is not there, the message says which install fixes it rather than
saying the file is binary. Refusing a PDF and refusing a JPEG are different
answers and only one of them is true.

What both of them are *not*: a converter. Neither renders a layout, and the
text of a PDF with two columns interleaves, because the format records where
each glyph is rather than which paragraph it is in. `pypdf` has layout mode for
that and it is off by default here — it is slow, and the slow version is the
one to reach for deliberately.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)

#: A spreadsheet past this is a data dump, not a document somebody asked a
#: question about. Two million cells is a CSV with a zip wrapper.
MAX_SHEET_CELLS = 2_000_000
#: Rows of a sheet kept. Enough to answer almost anything, small enough that a
#: dump does not become a context window.
MAX_SHEET_ROWS = 5_000
MAX_PDF_PAGES = 400

_DOC = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_SHEET_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_SHEET_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


class DocumentError(RuntimeError):
    """A document could not be read, said in words rather than as a type."""


@dataclass(slots=True)
class Read:
    """What was read, and what it was."""

    text: str
    kind: str
    pages: int = 0
    sheets: list[str] = field(default_factory=list)
    truncated: bool = False
    #: A warning that belongs next to the text rather than in place of it.
    note: str = ''

    def describe(self) -> str:
        bits = [self.kind]
        if self.pages:
            bits.append(f'{self.pages} page(s)')
        if self.sheets:
            bits.append('sheets: ' + ', '.join(self.sheets))
        if self.truncated:
            bits.append('truncated')
        return f"[{' — '.join(bits)}]\n\n{self.text}"


# ---------------------------------------------------------------------------
# Spreadsheets
# ---------------------------------------------------------------------------


def _col_of(ref: str) -> int:
    """`AB12` -> 27. The letters, base-26, one-based."""
    total = 0
    for char in ref:
        if not char.isalpha():
            break
        total = total * 26 + (ord(char.upper()) - 64)
    return max(0, total - 1)


def _clean(value: str) -> str:
    """A cell's text, with the escapes a spreadsheet file actually uses."""
    if not value:
        return ''
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    # `_x000D_` and friends: a leading underscore is how a file stores a
    # character that would otherwise be markup, and they show up in text
    # imported from other spreadsheets often enough to be worth taking out.
    value = re.sub(r'_x([0-9A-Fa-f]{4})_', lambda m: chr(int(m.group(1), 16)), value)
    return value


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Every string in the workbook, in the order the cells refer to them."""
    try:
        blob = archive.read('xl/sharedStrings.xml')
    except KeyError:
        # A workbook with none has every string inline, which is legal.
        return []
    try:
        root = ElementTree.fromstring(blob)
    except ElementTree.ParseError as exc:
        log.warning('sharedStrings.xml is not readable: %s', exc)
        return []
    out: list[str] = []
    for item in root.findall(f'{{{_SHEET_MAIN}}}si'):
        # A string is split across runs for formatting, and the text is the
        # concatenation of them — reading only the first run loses most of
        # every other cell.
        out.append(_clean(''.join(node.text or '' for node in item.iter(f'{{{_SHEET_MAIN}}}t'))))
    return out


def _sheet_names(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(display name, file path) for every sheet, in order."""
    try:
        root = ElementTree.fromstring(archive.read('xl/workbook.xml'))
    except (KeyError, ElementTree.ParseError) as exc:
        raise DocumentError(f'this does not look like a spreadsheet: {exc}') from exc

    rels = _relationships(archive)
    shared: list[tuple[str, str]] = []
    for rel in root.iter(f'{{{_SHEET_MAIN}}}sheet'):
        target = rel.get(f'{{{_SHEET_REL}}}id') or rel.get('id') or ''
        path = _resolve(archive, target, rels)
        if path:
            shared.append((rel.get('name') or 'Sheet', path))
    if not shared:
        # No relationship table, or one that names nothing usable: fall back to
        # the files themselves, which is a worse answer than the right one and
        # a better one than refusing.
        for name in sorted(n for n in archive.namelist() if n.startswith('xl/worksheets/')):
            shared.append((Path(name).stem, name))
    return shared


_REL_PACKAGE = 'http://schemas.openxmlformats.org/package/2006/relationships'


def _relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    """Relationship id -> target, from `xl/_rels/workbook.xml.rels`.

    A workbook's `<sheet r:id="rId1">` names a *relationship*, not a file.
    The first version of this treated the id as a path, every lookup missed,
    and every sheet came out named `sheet1`, `sheet2` — from a fallback that
    happened to produce readable output, which is why it was not obviously
    broken. Real workbooks name their sheets; that is the content.
    """
    try:
        blob = archive.read('xl/_rels/workbook.xml.rels')
    except (KeyError, OSError):
        return {}
    try:
        root = ElementTree.fromstring(blob)
    except ElementTree.ParseError as exc:
        log.warning('workbook.xml.rels is not readable: %s', exc)
        return {}
    out: dict[str, str] = {}
    for node in root.findall(f'{{{_REL_PACKAGE}}}Relationship'):
        target, rel_id = node.get('Target'), node.get('Id')
        if target and rel_id:
            out[rel_id] = target
    return out


def _resolve(archive: zipfile.ZipFile, target: str, rels: dict[str, str] | None = None) -> str:
    """A sheet's `r:id` or a direct path, turned into a file in the archive.

    Relationships are usually relative to `xl/`, which is where the workbook
    lives, so a target of `worksheets/sheet1.xml` is `xl/worksheets/sheet1.xml`.
    """
    if not target:
        return ''
    if (rels or {}).get(target):
        target = rels[target]  # type: ignore[index]
    if target.startswith('/'):
        candidate = target[1:]
    elif target.startswith('xl/'):
        candidate = target
    else:
        candidate = f'xl/{target}'
    return candidate if candidate in archive.namelist() else ''


def read_xlsx(data: bytes, *, max_rows: int = MAX_SHEET_ROWS) -> Read:
    """Every sheet as text, one table per sheet.

    Cells are placed by column so a row reads as a row. A spreadsheet is
    mostly empty cells and a naive reader either drops them — losing every
    column alignment, which is the whole content of a table — or prints a wall
    of blanks.
    """
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentError(f'this is not a readable spreadsheet: {exc}') from exc

    with archive:
        strings = _shared_strings(archive)
        sheets = _sheet_names(archive)
        if not sheets:
            raise DocumentError('this spreadsheet has no sheets in it')

        chunks: list[str] = []
        names: list[str] = []
        truncated = False
        for name, path in sheets:
            names.append(name)
            try:
                blob = archive.read(path)
            except KeyError:
                continue
            try:
                root = ElementTree.fromstring(blob)
            except ElementTree.ParseError as exc:
                chunks.append(f'## {name}\n\n(sheet not readable: {exc})')
                continue
            rows = root.iter(f'{{{_SHEET_MAIN}}}row')
            out: list[str] = []
            count = 0
            for row in rows:
                if count >= max_rows:
                    truncated = True
                    break
                cells: dict[int, str] = {}
                for cell in row.findall(f'{{{_SHEET_MAIN}}}c'):
                    ref = cell.get('r') or ''
                    kind = cell.get('t') or 'n'
                    value_node = cell.find(f'{{{_SHEET_MAIN}}}v')
                    inline = cell.find(f'{{{_SHEET_MAIN}}}is')
                    if kind == 's':
                        index = int(value_node.text or 0) if value_node is not None and value_node.text else -1
                        text = strings[index] if 0 <= index < len(strings) else ''
                    elif kind == 'inlineStr':
                        text = _clean(''.join(
                            n.text or '' for n in (inline.iter(f'{{{_SHEET_MAIN}}}t') if inline is not None else [])
                        ))
                    elif kind == 'str':
                        text = _clean(value_node.text or '') if value_node is not None else ''
                    else:
                        text = value_node.text if value_node is not None and value_node.text else ''
                    if text:
                        cells[_col_of(ref) if ref else len(cells)] = text
                if cells:
                    out.append('\t'.join(cells.get(i, '') for i in range(max(cells) + 1)))
                    count += 1
            chunks.append(f'## {name}\n\n' + ('\n'.join(out) if out else '(empty)'))

    return Read(
        text='\n\n'.join(chunks), kind='spreadsheet', sheets=names, truncated=truncated,
        note='cells are tab-separated; empty cells are dropped, so a row reads as its own values',
    )


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------


def pdf_available() -> bool:
    try:
        import pypdf  # noqa: F401

        return True
    except ImportError:
        return False


def read_pdf(data: bytes, *, max_pages: int = MAX_PDF_PAGES, layout: bool = False) -> Read:
    """The text of a PDF, page by page.

    Needs `pypdf`, and says so by name when it is missing. A PDF refused as
    "binary" is a lie — the file is a document, this just cannot read it yet,
    and the difference is the difference between a bug report and an install
    command.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError(
            'reading a PDF needs one more library: pip install \'openmirror[docs]\'. '
            'Everything else reads without it.'
        ) from exc

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide variety
        raise DocumentError(f'this could not be opened as a PDF: {exc}') from exc

    chunks: list[str] = []
    truncated = False
    for number, page in enumerate(reader.pages[:max_pages], 1):
        try:
            text = page.extract_text(extraction_mode='layout') if layout else page.extract_text()
        except Exception:  # noqa: BLE001 - one unreadable page is not a whole document
            text = ''
        chunks.append(f'--- page {number} ---\n{text.strip()}')
    if len(reader.pages) > max_pages:
        truncated = True

    return Read(
        text='\n\n'.join(chunks), kind='pdf', pages=len(chunks), truncated=truncated,
        note=(
            'this is the text as it is positioned on the page, not a reflowed document — '
            'a two-column PDF interleaves'
            if not layout else ''
        ),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def read_document(path: Path, *, layout: bool = False) -> Read | None:
    """Read a document by extension, or None when it is not one."""
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        return read_pdf(path.read_bytes(), layout=layout)
    if suffix in ('.xlsx', '.xlsm'):
        return read_xlsx(path.read_bytes())
    return None


def is_document(path: Path) -> bool:
    return path.suffix.lower() in ('.pdf', '.xlsx', '.xlsm')


__all__ = [
    'DocumentError', 'MAX_PDF_PAGES', 'MAX_SHEET_ROWS', 'Read', 'is_document', 'pdf_available',
    'read_document', 'read_pdf', 'read_xlsx',
]
