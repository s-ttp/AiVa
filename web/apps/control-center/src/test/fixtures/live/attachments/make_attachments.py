"""Writes the synthetic attachment fixtures for /live's file reader (attachment.test.ts, e2e/live.spec.ts).

Every file is built by hand from fictional demo data, byte for byte the same on every run:

- `two-pages.pdf`: two pages of text in Helvetica.
- `scanned.pdf`: one page that holds only an image, as a scan does: no text layer.
- `two-sheets.xlsx`: sheets "Tariffs" and "Usage" with shared and inline strings, a date, a percentage,
  a formula with its cached value, a boolean and an empty cell.
- `tariffs.csv`: a header and rows, one with a quoted comma and one with a quoted line break.

    uv run python web/apps/control-center/src/test/fixtures/live/attachments/make_attachments.py
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pdf(objects: list[bytes]) -> bytes:
    """A PDF from its numbered objects (1-based), with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def stream(content: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length " + str(len(content)).encode() + extra + b" >>\nstream\n" + content + b"\nendstream"


def page(resources: bytes, contents: int) -> bytes:
    return (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        + resources
        + b" /Contents "
        + str(contents).encode()
        + b" 0 R >>"
    )


def text(first: bytes, second: bytes) -> bytes:
    return b"BT /F1 16 Tf 72 720 Td (" + first + b") Tj 0 -24 Td (" + second + b") Tj ET"


def two_pages() -> bytes:
    fonts = b"<< /Font << /F1 7 0 R >> >>"
    return pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
            page(fonts, 4),
            stream(text(b"GCT roaming tariffs, synthetic", b"Gulf Day Pass: 35 AED for 24 hours")),
            page(fonts, 6),
            stream(text(b"Page two: fair use", b"1 GB at full speed, then 256 kbps")),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        ]
    )


def scanned() -> bytes:
    image = b" /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8"
    return pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            page(b"<< /XObject << /Im1 5 0 R >> >>", 4),
            stream(b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
            stream(bytes([0x80, 0x40, 0x40, 0x80]), image),
        ]
    )


XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPREADSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml"


def relationships(*items: tuple[str, str, str]) -> str:
    rels = "".join(
        f'<Relationship Id="{rid}" Type="{OFFICE_RELS}/{kind}" Target="{target}"/>'
        for rid, kind, target in items
    )
    return f'{XML}<Relationships xmlns="{PACKAGE_RELS}">{rels}</Relationships>'


def override(part: str, kind: str) -> str:
    return f'<Override PartName="{part}" ContentType="{SPREADSHEET}.{kind}+xml"/>'


def sheet(*rows: str) -> str:
    return f"{XML}<worksheet {SHEET_NS}><sheetData>{''.join(rows)}</sheetData></worksheet>"


def row(number: int, *cells: str) -> str:
    return f'<row r="{number}">{"".join(cells)}</row>'


def cell(ref: str, value: str, attributes: str = "", formula: str = "") -> str:
    inner = (f"<f>{formula}</f>" if formula else "") + f"<v>{value}</v>"
    return f'<c r="{ref}"{attributes}>{inner}</c>'


def inline(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


XLSX_PARTS = {
    "[Content_Types].xml": (
        f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + override("/xl/workbook.xml", "sheet.main")
        + override("/xl/worksheets/sheet1.xml", "worksheet")
        + override("/xl/worksheets/sheet2.xml", "worksheet")
        + override("/xl/sharedStrings.xml", "sharedStrings")
        + override("/xl/styles.xml", "styles")
        + "</Types>"
    ),
    "_rels/.rels": (
        f'{XML}<Relationships xmlns="{PACKAGE_RELS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_RELS}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ),
    "xl/workbook.xml": (
        f"{XML}<workbook {SHEET_NS} {REL_NS}><sheets>"
        '<sheet name="Tariffs" sheetId="1" r:id="rId1"/><sheet name="Usage" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    ),
    "xl/_rels/workbook.xml.rels": relationships(
        ("rId1", "worksheet", "worksheets/sheet1.xml"),
        ("rId2", "worksheet", "/xl/worksheets/sheet2.xml"),
        ("rId3", "sharedStrings", "sharedStrings.xml"),
        ("rId4", "styles", "styles.xml"),
    ),
    "xl/sharedStrings.xml": (
        f'{XML}<sst {SHEET_NS} count="6" uniqueCount="6">'
        "<si><t>Bundle</t></si><si><t>Price (AED)</t></si><si><t>Valid from</t></si>"
        '<si><r><t>Gulf Day</t></r><r><t xml:space="preserve"> Pass</t></r></si>'
        "<si><t>Discount</t></si><si><t>Week Pass, GCC</t></si>"
        "</sst>"
    ),
    # cellXfs: 0 General, 1 date (built-in 14), 2 percent (built-in 10, 0.00%), 3 custom "#,##0.00".
    "xl/styles.xml": (
        f"{XML}<styleSheet {SHEET_NS}>"
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
        '<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/>'
        '<xf numFmtId="10" applyNumberFormat="1"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs>'
        "</styleSheet>"
    ),
    "xl/worksheets/sheet1.xml": sheet(
        row(
            1,
            *(cell(f"{col}1", str(index), ' t="s"') for col, index in zip("ABCD", (0, 1, 2, 4), strict=True)),
        ),
        row(
            2,
            cell("A2", "3", ' t="s"'),
            cell("B2", "35", ' s="3"'),
            cell("C2", "46316", ' s="1"'),
            cell("D2", "0.125", ' s="2"'),
        ),
        row(
            3, cell("A3", "5", ' t="s"'), cell("B3", "1400.5", ' s="3"', "B2*4"), cell("D3", "0.2", ' s="2"')
        ),
    ),
    "xl/worksheets/sheet2.xml": sheet(
        row(1, inline("A1", "Subscriber"), inline("B1", "Roaming")),
        row(2, inline("A2", "SYN-0001"), cell("B2", "1", ' t="b"')),
        row(3, cell("A3", "SYN-0002", ' t="str"', '"SYN-"&amp;"0002"'), cell("B3", "0", ' t="b"')),
    ),
}


def two_sheets() -> None:
    with zipfile.ZipFile(HERE / "two-sheets.xlsx", "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in XLSX_PARTS.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, xml)


TARIFFS_CSV = (
    "Bundle,Price (AED),Notes\n"
    "Gulf Day Pass,35,24 hours\n"
    '"Week Pass, GCC",120,"Seven days, all six GCC states"\n'
    'Month Pass,300,"Fair use:\n1 GB a day"\n'
)


def main() -> None:
    (HERE / "two-pages.pdf").write_bytes(two_pages())
    (HERE / "scanned.pdf").write_bytes(scanned())
    two_sheets()
    (HERE / "tariffs.csv").write_text(TARIFFS_CSV, encoding="utf-8", newline="")
    for path in sorted(HERE.iterdir()):
        if path.suffix != ".py":
            print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
