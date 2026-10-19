"""Problem 5: find content in a vendor document that a person would not see but a text extractor would.

    python scripts/inspect_docs.py data/practice_docs/p02_elmcity_quote.html [more files ...]

Read-only, no AI calls. For each file it reports, with locations in the original file:
    HTML  comments, <script> text, and elements whose inline style hides them (display:none, visibility:hidden,
          opacity:0, font-size 0-2px, white or near-white text, off-screen positioning), plus zero-width characters
    EML   every MIME part (type, encoding), text that appears only in the HTML part, and the HTML findings above
          for that part (line numbers count from the start of the decoded HTML part)
    PDF   every text run with its page position, font size, fill color and render mode, flagging white or
          near-white fill, font size under 3 pt, invisible render mode (3) and text placed off the page
"""

from __future__ import annotations

import json
import re
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from bs4.element import Script
from pypdf import PdfReader
from pypdf.generic import ContentStream

ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
WHITE = {"white", "#fff", "#ffffff", "rgb(255,255,255)", "rgb(255, 255, 255)"}


def _style(tag) -> dict[str, str]:
    decls = {}
    for part in (tag.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip().lower()] = v.strip().lower()
    return decls


def _hiding_reasons(decls: dict[str, str]) -> list[str]:
    reasons = []
    if decls.get("display") == "none":
        reasons.append("display:none")
    if decls.get("visibility") == "hidden":
        reasons.append("visibility:hidden")
    if decls.get("opacity") in ("0", "0.0"):
        reasons.append("opacity:0")
    size = re.match(r"([\d.]+)\s*(px|pt|em|rem)?", decls.get("font-size", ""))
    if size and float(size.group(1)) < (0.2 if size.group(2) in ("em", "rem") else 3):
        reasons.append(f"font-size:{decls['font-size']}")
    color = decls.get("color", "").replace(" ", "")
    if color in {c.replace(" ", "") for c in WHITE} or re.fullmatch(r"#f[ef]f[ef]f[ef]|#f[0-9a-f]f[0-9a-f]f[0-9a-f]", color):
        reasons.append(f"color:{decls['color']} (white on a white page)")
    for side in ("left", "top", "text-indent"):
        m = re.match(r"-(\d+)", decls.get(side, ""))
        if m and int(m.group(1)) >= 1000:
            reasons.append(f"{side}:{decls[side]} (off-screen)")
    return reasons


def inspect_html(markup: str) -> dict:
    soup = BeautifulSoup(markup, "html.parser")
    lines = markup.splitlines()
    findings = []
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        pos = markup.find(f"<!--{node}-->")
        findings.append({"kind": "html_comment", "line": markup.count("\n", 0, pos) + 1 if pos >= 0 else None,
                         "text": node.strip()})
    for node in soup.find_all(string=lambda s: isinstance(s, Script)):
        findings.append({"kind": "script_text", "line": node.parent.sourceline, "text": node.strip()})
    for tag in soup.find_all(True):
        reasons = _hiding_reasons(_style(tag))
        if reasons and tag.get_text(strip=True):
            findings.append({"kind": "hidden_element", "line": tag.sourceline, "tag": tag.name,
                             "style": tag.get("style"), "hidden_by": reasons, "text": tag.get_text(" ", strip=True)})
    zw = ZERO_WIDTH.findall(markup)
    return {"findings": findings, "zero_width_characters": len(zw), "line_count": len(lines)}


def inspect_eml(data: bytes) -> dict:
    msg = BytesParser(policy=policy.default).parsebytes(data)
    parts, plain, html = [], "", ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        body = part.get_content() if part.get_content_maintype() == "text" else ""
        parts.append({"content_type": ctype, "transfer_encoding": part.get("Content-Transfer-Encoding"),
                      "decoded_chars": len(body)})
        if ctype == "text/plain":
            plain += body
        elif ctype == "text/html":
            html += body
    out = {"headers": {k: v for k, v in msg.items() if not k.lower().startswith(("content-", "mime-version"))},
           "parts": parts, "html_part": inspect_html(html) if html else None}
    if html:
        html_text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
        plain_text = " ".join(plain.split())
        out["html_only_text"] = [s for s in re.split(r"(?<=[.!?])\s+", html_text) if s and s not in plain_text]
    return out


def inspect_pdf(data: bytes) -> dict:
    reader = PdfReader(__import__("io").BytesIO(data))
    runs = []
    for page_no, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        fill, size, mode, x, y = (0.0, 0.0, 0.0), None, 0, 0.0, 0.0
        stack = []
        for operands, op in ContentStream(page.get_contents(), reader).operations:
            if op == b"q":
                stack.append((fill, mode))
            elif op == b"Q" and stack:
                fill, mode = stack.pop()
            elif op == b"rg":
                fill = tuple(float(v) for v in operands)
            elif op == b"g":
                fill = (float(operands[0]),) * 3
            elif op == b"k":
                c, m, yk, k = (float(v) for v in operands)
                fill = tuple((1 - min(1, v + k)) for v in (c, m, yk))
            elif op == b"Tf":
                size = float(operands[1])
            elif op == b"Tr":
                mode = int(operands[0])
            elif op == b"Tm":
                x, y = float(operands[4]), float(operands[5])
            elif op in (b"Td", b"TD"):
                x, y = x + float(operands[0]), y + float(operands[1])
            elif op in (b"Tj", b"'", b'"', b"TJ"):
                arg = operands[-1]
                text = "".join(str(t) for t in arg if not isinstance(t, (int, float))) if op == b"TJ" else str(arg)
                if not text.strip():
                    continue
                flags = []
                if all(c >= 0.9 for c in fill):
                    flags.append(f"near-white fill {tuple(round(c, 3) for c in fill)}")
                if size is not None and size < 3:
                    flags.append(f"font size {size} pt")
                if mode == 3:
                    flags.append("render mode 3 (invisible)")
                if not (float(box.left) <= x <= float(box.right) and float(box.bottom) <= y <= float(box.top)):
                    flags.append("outside the page")
                runs.append({"page": page_no, "x": x, "y": y, "font_size": size, "fill_rgb": [round(c, 3) for c in fill],
                             "render_mode": mode, "text": text, "hidden_by": flags})
    return {"text_runs": runs, "hidden_runs": [r for r in runs if r["hidden_by"]],
            "metadata": {k: str(v) for k, v in (reader.metadata or {}).items()}}


def inspect(path: str | Path) -> dict:
    file = Path(path)
    data = file.read_bytes()
    suffix = file.suffix.lower()
    if suffix in (".html", ".htm"):
        result = inspect_html(data.decode("utf-8", errors="replace"))
    elif suffix == ".eml":
        result = inspect_eml(data)
    elif suffix == ".pdf":
        result = inspect_pdf(data)
    else:
        raise ValueError(f"unsupported file type {suffix}")
    return {"file": file.name, **result}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps([inspect(p) for p in sys.argv[1:]], indent=2, ensure_ascii=False))
