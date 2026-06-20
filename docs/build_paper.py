"""Build paper.html and paper.pdf from paper.md.

Academic-style layout: serif font, justified body, numbered sections, page
numbers in footer, conservative use of color. Designed for both screen
reading (HTML) and print (PDF via xhtml2pdf).

Usage (from inside docs/):
    python build_paper.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import markdown
from xhtml2pdf import pisa

HERE = Path(__file__).resolve().parent
SRC = HERE / "paper.md"
HTML_OUT = HERE / "paper.html"
PDF_OUT = HERE / "paper.pdf"

# Academic-style CSS. Times-family serif, justified body, restrained color.
# xhtml2pdf only supports a subset of CSS — no flexbox, limited selectors.
CSS = r"""
@page {
    size: A4;
    margin: 2cm 2.2cm 2.5cm 2.2cm;
    @frame footer {
        -pdf-frame-content: footerContent;
        bottom: 1.2cm;
        margin-left: 2.2cm;
        margin-right: 2.2cm;
        height: 1cm;
    }
}
body {
    font-family: "Times New Roman", "Times", serif;
    font-size: 10pt;
    line-height: 1.45;
    color: #111827;
    text-align: justify;
}
h1 {
    font-family: "Times New Roman", "Times", serif;
    font-size: 17pt;
    text-align: center;
    margin-top: 0.4em;
    margin-bottom: 0.4em;
    color: #111827;
    page-break-after: avoid;
}
h2 {
    font-family: "Times New Roman", "Times", serif;
    font-size: 13pt;
    color: #111827;
    margin-top: 1.2em;
    margin-bottom: 0.3em;
    page-break-after: avoid;
}
h3 {
    font-family: "Times New Roman", "Times", serif;
    font-size: 11pt;
    font-style: italic;
    color: #111827;
    margin-top: 0.9em;
    margin-bottom: 0.2em;
    page-break-after: avoid;
}
h4 {
    font-family: "Times New Roman", "Times", serif;
    font-size: 10pt;
    font-weight: bold;
    color: #111827;
    margin-top: 0.7em;
    margin-bottom: 0.2em;
    page-break-after: avoid;
}
p { margin: 0.35em 0; }
ul, ol { margin: 0.4em 0 0.4em 1.3em; padding: 0; text-align: left; }
li { margin: 0.15em 0; }
a { color: #1d4ed8; text-decoration: none; }
strong { font-weight: bold; }
em { font-style: italic; }
code {
    font-family: "Courier New", "Consolas", monospace;
    font-size: 9pt;
    background: #f3f4f6;
    border: 1px solid #e5e7eb;
    border-radius: 2px;
    padding: 0px 3px;
}
pre {
    font-family: "Courier New", "Consolas", monospace;
    font-size: 8pt;
    background: #f8fafc;
    color: #1f2937;
    border: 1px solid #e5e7eb;
    border-radius: 3px;
    padding: 6px 8px;
    margin: 0.5em 0;
    line-height: 1.25;
    page-break-inside: avoid;
    text-align: left;
}
pre code {
    background: transparent;
    border: 0;
    padding: 0;
    color: inherit;
    font-size: 8pt;
}
blockquote {
    border-left: 2px solid #9ca3af;
    color: #374151;
    padding: 2px 10px;
    margin: 0.5em 0;
    font-style: italic;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 0.7em 0;
    font-size: 9pt;
    page-break-inside: avoid;
}
th, td {
    border: 1px solid #cbd5e1;
    padding: 4px 7px;
    text-align: left;
    vertical-align: top;
}
th {
    background: #f3f4f6;
    color: #111827;
    font-weight: bold;
}
hr {
    border: 0;
    border-top: 1px solid #cbd5e1;
    margin: 1.2em 0;
}
.footer { font-size: 8.5pt; color: #6b7280; text-align: center; font-family: "Times New Roman", "Times", serif; }
"""

PDF_FOOTER = """
<div id="footerContent" class="footer">
    Smriti, M. (2026) · Conversational Geospatial Analytics via GEE and the MCP · page <pdf:pagenumber/> of <pdf:pagecount/>
</div>
"""


def build():
    if not SRC.exists():
        sys.exit(f"Source not found: {SRC}")

    md_text = SRC.read_text(encoding="utf-8")

    extensions = ["extra", "tables", "fenced_code", "toc", "codehilite", "attr_list"]
    extension_configs = {
        "codehilite": {"guess_lang": False, "noclasses": True, "pygments_style": "friendly"},
        "toc": {"permalink": False, "anchorlink": False},
    }
    html_body = markdown.markdown(md_text, extensions=extensions, extension_configs=extension_configs)

    # Web HTML — readable line length for screen.
    html_full = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Conversational Geospatial Analytics via GEE and the MCP</title>
<style>{CSS}
body {{ max-width: 760px; margin: 2em auto; padding: 0 1em; font-size: 11pt; line-height: 1.55; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    HTML_OUT.write_text(html_full, encoding="utf-8")
    print(f"Wrote {HTML_OUT}")

    pdf_html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{CSS}</style>
</head>
<body>
{PDF_FOOTER}
{html_body}
</body>
</html>"""
    with PDF_OUT.open("wb") as out:
        result = pisa.CreatePDF(pdf_html, dest=out, encoding="utf-8")
    if result.err:
        sys.exit(f"PDF generation failed with {result.err} errors.")
    print(f"Wrote {PDF_OUT}  ({PDF_OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build()
