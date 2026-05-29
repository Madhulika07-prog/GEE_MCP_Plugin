"""Build setup-guide.html and setup-guide.pdf from setup-guide.md.

Usage (from inside docs/):
    python build_docs.py

Produces:
    docs/setup-guide.html  — for web browsing
    docs/setup-guide.pdf   — for printing / archiving
"""

from __future__ import annotations

import sys
from pathlib import Path

import markdown
from xhtml2pdf import pisa

HERE = Path(__file__).resolve().parent
SRC = HERE / "setup-guide.md"
HTML_OUT = HERE / "setup-guide.html"
PDF_OUT = HERE / "setup-guide.pdf"

# Stylesheet shared by HTML and PDF outputs. Kept conservative because xhtml2pdf
# only supports a subset of CSS — no flexbox, limited selectors, no shorthand.
CSS = r"""
@page {
    size: A4;
    margin: 1.5cm 1.8cm 2cm 1.8cm;
    @frame footer {
        -pdf-frame-content: footerContent;
        bottom: 1cm;
        margin-left: 1.8cm;
        margin-right: 1.8cm;
        height: 1cm;
    }
}
body {
    font-family: "Helvetica", "Arial", sans-serif;
    font-size: 10pt;
    line-height: 1.5;
    color: #1f2937;
}
h1 {
    color: #1d4ed8;
    border-bottom: 2px solid #1d4ed8;
    padding-bottom: 0.2em;
    margin-top: 1em;
    font-size: 22pt;
    page-break-after: avoid;
}
h2 {
    color: #1e40af;
    border-bottom: 1px solid #cbd5e1;
    padding-bottom: 0.15em;
    margin-top: 1.4em;
    font-size: 16pt;
    page-break-after: avoid;
    page-break-before: auto;
}
h3 {
    color: #1e3a8a;
    margin-top: 1.1em;
    font-size: 13pt;
    page-break-after: avoid;
}
h4 {
    color: #1e3a8a;
    font-size: 11pt;
    margin-top: 1em;
    page-break-after: avoid;
}
p, li { margin: 0.4em 0; }
ul, ol { margin: 0.4em 0 0.4em 1.4em; padding: 0; }
li { margin: 0.2em 0; }
a { color: #1d4ed8; text-decoration: none; }
strong { font-weight: bold; }
em { font-style: italic; }
code {
    font-family: "Courier New", "Consolas", monospace;
    font-size: 9pt;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 2px;
    padding: 1px 3px;
}
pre {
    font-family: "Courier New", "Consolas", monospace;
    font-size: 8.5pt;
    background: #0f172a;
    color: #e2e8f0;
    padding: 8px 10px;
    border-radius: 3px;
    margin: 0.6em 0;
    page-break-inside: avoid;
}
pre code {
    background: transparent;
    border: 0;
    padding: 0;
    color: inherit;
    font-size: 8.5pt;
}
blockquote {
    border-left: 3px solid #38bdf8;
    background: #f0f9ff;
    color: #0c4a6e;
    padding: 6px 10px;
    margin: 0.6em 0;
    font-style: italic;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 0.6em 0;
    font-size: 9pt;
    page-break-inside: avoid;
}
th, td {
    border: 1px solid #cbd5e1;
    padding: 5px 8px;
    text-align: left;
    vertical-align: top;
}
th {
    background: #e0f2fe;
    color: #0c4a6e;
    font-weight: bold;
}
tr:nth-child(even) td { background: #f8fafc; }
hr {
    border: 0;
    border-top: 1px solid #cbd5e1;
    margin: 1.2em 0;
}
.toc { background: #f8fafc; border: 1px solid #e2e8f0; padding: 8px 14px; margin: 1em 0; }
.toc ol { margin: 0.2em 0 0.2em 1.4em; }
.footer { font-size: 8pt; color: #64748b; text-align: center; }
"""

# Footer template — xhtml2pdf uses a special frame for repeating footers.
PDF_FOOTER = """
<div id="footerContent" class="footer">
    GEE_MCP_Plugin Setup Guide · github.com/Madhulika07-prog/GEE_MCP_Plugin · page <pdf:pagenumber/> of <pdf:pagecount/>
</div>
"""


def build():
    if not SRC.exists():
        sys.exit(f"Source not found: {SRC}")

    md_text = SRC.read_text(encoding="utf-8")

    # Markdown -> HTML body.
    extensions = ["extra", "tables", "fenced_code", "toc", "codehilite", "attr_list"]
    extension_configs = {
        "codehilite": {"guess_lang": False, "noclasses": True, "pygments_style": "friendly"},
        "toc": {"permalink": False, "anchorlink": False},
    }
    html_body = markdown.markdown(md_text, extensions=extensions, extension_configs=extension_configs)

    # Browser-facing HTML — full doc.
    html_full = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GEE_MCP_Plugin — Setup Guide</title>
<style>{CSS}
body {{ max-width: 880px; margin: 2em auto; padding: 0 1em; font-size: 11pt; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    HTML_OUT.write_text(html_full, encoding="utf-8")
    print(f"Wrote {HTML_OUT}")

    # PDF — adds repeating footer frame; uses tighter base font size.
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
