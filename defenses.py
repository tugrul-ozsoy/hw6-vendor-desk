"""Problem 6: three defense layers for the vendor desk agent, each switchable for ablation runs.

CODE    sanitize_document(path): send the model only what a person would see.
        - HTML: drops comments, <script>/<style>/<head>/<template>/<noscript> and embedded objects, and text hidden
          by CSS (inline styles and simple <style> rules): display:none, visibility:hidden, opacity:0, font-size under
          3 px, text color matching its background, transparent text, off-screen or clipped boxes, the hidden
          attribute. Attribute text (alt, title, ...) is not sent.
        - Email: only what a mail client shows: From/To/Cc/Subject/Date, then the displayed body (the HTML part
          through the HTML rules, else the plain part), and PDF/HTML/text attachments through their own rules.
          Text that exists only in the non-displayed alternative part is withheld.
        - PDF: only text drawn readably on the page: not white or near-white, at least 3 pt, not an invisible render
          mode, inside the page box. Annotations, form fields and metadata are withheld.
        - All text: Unicode NFKC normalization; invisible format characters (zero-width, bidi controls) removed.
        Everything withheld is recorded (QuarantinedItem) for the audit trail and never sent to the model.
        Instruction-like phrases in the visible text are flagged for review, not removed.
DESIGN  - envelope(): the document reaches the model inside markers with a random per-run nonce, labeled as
          untrusted data.
        - gated send_email / approve_payment: same names and schemas as the tools in tools.py, but a call made during
          a document run is refused and logged to output/blocked_actions.json. The real tools run only through
          human_approve(), which needs a named staff member.
        - PolicyEngine: code applies the handbook's four checks to the extracted facts and makes the decision.
        - ground(): a fact the model reports must appear in the document text it was given; otherwise it is treated
          as unknown.
        - clean_output(): links, email addresses and evidence that are not in the visible document are removed from
          the answer.
PROMPT  prompts/prompt.md (defended) instead of prompts/prompt_baseline.md (the original Problem 3 prompt).
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from email import policy as email_policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction
from pydantic_ai import RunContext
from pypdf import PdfReader

from models import (
    AgentDeps, BlockedAction, Decision, QuarantinedItem, SanitizedDocument, TrapCall, VendorDeskResult,
    VendorDocument,
)  # fmt: skip
from tools import FORMATS, OUTPUT, TRAP_CALLS, append_json, project_path, read_json_array, utc_now, _write_json, _LOG_LOCK

BLOCKED_ACTIONS = OUTPUT / "blocked_actions.json"


@dataclass(frozen=True)
class DefenseConfig:
    name: str
    code: bool
    design: bool
    prompt: bool

    def layers(self) -> dict[str, bool]:
        return {"code": self.code, "design": self.design, "prompt": self.prompt}


PRESETS = {
    "none": DefenseConfig("none", False, False, False),  # the original Problem 3 agent
    "code": DefenseConfig("code", True, False, False),
    "code+design": DefenseConfig("code+design", True, True, False),
    "full": DefenseConfig("full", True, True, True),
    "design": DefenseConfig("design", False, True, False),  # single-layer configs, for effectiveness tests
    "prompt": DefenseConfig("prompt", False, False, True),
}


# =====================================================================================================
# CODE LAYER
# =====================================================================================================


def normalize_text(text: str) -> tuple[str, int]:
    """NFKC-normalize, drop invisible format/control characters, collapse spaces. Returns (text, removed)."""
    text = unicodedata.normalize("NFKC", text)
    kept, removed = [], 0
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cf" or (cat == "Cc" and ch not in "\n\t"):
            removed += 1
            continue
        kept.append(ch)
    lines = (" ".join(line.split()) for line in "".join(kept).splitlines())
    return "\n".join(line for line in lines if line), removed


def _excerpt(text: str, n: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------- HTML ----------

NON_RENDERED = ["script", "style", "template", "noscript", "head", "iframe", "object", "embed", "svg", "canvas"]
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "label", "legend", "li", "main", "menu", "nav", "ol", "option", "p", "pre", "section", "summary", "table", "tr",
    "ul",
}  # fmt: skip
NAMED_COLORS = {
    "white": (255, 255, 255), "black": (0, 0, 0), "snow": (255, 250, 250), "ivory": (255, 255, 240),
    "whitesmoke": (245, 245, 245), "ghostwhite": (248, 248, 255), "floralwhite": (255, 250, 240),
    "seashell": (255, 245, 238), "mintcream": (245, 255, 250), "azure": (240, 255, 255), "aliceblue": (240, 248, 255),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192), "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211), "gainsboro": (220, 220, 220), "red": (255, 0, 0), "blue": (0, 0, 255),
    "navy": (0, 0, 128), "green": (0, 128, 0),
}  # fmt: skip


def _decls(style: str) -> dict[str, str]:
    out = {}
    for part in re.sub(r"/\*.*?\*/", "", style or "", flags=re.S).split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return out


def _css_rules(soup) -> list[tuple[str, dict[str, str]]]:
    """Simple selectors (tag, .class, #id, and combinations) from <style> blocks."""
    rules = []
    for style in soup.find_all("style"):
        css = re.sub(r"/\*.*?\*/", "", style.get_text(), flags=re.S)
        for sel_block, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            decls = _decls(body)
            for sel in sel_block.split(","):
                sel = sel.strip()
                if re.fullmatch(r"[a-zA-Z][\w-]*|(?:[a-zA-Z][\w-]*)?(?:[.#][\w-]+)+", sel):
                    rules.append((sel, decls))
    return rules


def _matches(tag, sel: str) -> bool:
    m = re.fullmatch(r"([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)", sel)
    if not m:
        return False
    name, rest = m.group(1), m.group(2)
    if name and tag.name != name.lower():
        return False
    classes = set(tag.get("class") or [])
    for kind, value in re.findall(r"([.#])([\w-]+)", rest):
        if kind == "." and value not in classes:
            return False
        if kind == "#" and tag.get("id") != value:
            return False
    return True


def _color(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    v = value.strip().lower().split()[0] if value.strip() else ""
    if v == "transparent":
        return (0, 0, 0, 0)
    if v in NAMED_COLORS:
        return (*NAMED_COLORS[v], 1)
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1
        return (r, g, b, a)
    m = re.match(r"rgba?\(([^)]*)\)", value.replace(" ", ""))
    if m:
        parts = [p for p in re.split(r"[,/]", m.group(1)) if p]
        nums = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p) for p in parts[:3]]
        a = float(parts[3].rstrip("%")) / (100 if parts[3].endswith("%") else 1) if len(parts) > 3 else 1
        return (*nums, a) if len(nums) == 3 else None
    return None


def _px(value: str | None, parent_px: float = 16.0) -> float | None:
    if not value:
        return None
    m = re.match(r"(-?[\d.]+)\s*(px|pt|em|rem|%)?", value)
    if not m:
        return {"xx-small": 9, "x-small": 10, "small": 13, "medium": 16}.get(value)
    n, unit = float(m.group(1)), m.group(2) or "px"
    return {"px": n, "pt": n * 4 / 3, "em": n * parent_px, "rem": n * 16, "%": n * parent_px / 100}[unit]


class _Styles:
    """Effective CSS for an element: matching <style> rules, then the inline style (inline wins)."""

    def __init__(self, soup):
        self.rules = _css_rules(soup)
        self.cache: dict[int, dict[str, str]] = {}

    def of(self, tag) -> dict[str, str]:
        key = id(tag)
        if key not in self.cache:
            merged = {}
            for sel, decls in self.rules:
                if _matches(tag, sel):
                    merged.update(decls)
            merged.update(_decls(tag.get("style")))
            self.cache[key] = merged
        return self.cache[key]

    def inherited(self, tag, prop: str) -> str | None:
        while tag is not None and getattr(tag, "name", None):
            value = self.of(tag).get(prop)
            if value:
                return value
            tag = tag.parent
        return None

    def font_px(self, tag) -> float:
        chain = []
        while tag is not None and getattr(tag, "name", None):
            chain.append(tag)
            tag = tag.parent
        size = 16.0
        for t in reversed(chain):
            px = _px(self.of(t).get("font-size"), size)
            if px is not None:
                size = px
        return size

    def background(self, tag) -> tuple[float, float, float]:
        while tag is not None and getattr(tag, "name", None):
            d = self.of(tag)
            for prop in ("background-color", "background"):
                for token in (d.get(prop) or "").split():
                    c = _color(token)
                    if c and c[3] > 0:
                        return c[:3]
            tag = tag.parent
        return (255, 255, 255)


def _hidden_reasons(node, styles: _Styles) -> list[str]:
    parent = node.parent
    reasons = []
    el = parent
    while el is not None and getattr(el, "name", None):
        d = styles.of(el)
        if d.get("display") == "none":
            reasons.append(f"display:none on <{el.name}>")
        if el.has_attr("hidden"):
            reasons.append(f"hidden attribute on <{el.name}>")
        if re.fullmatch(r"0*(\.0*)?%?", d.get("opacity", "1").strip() or "1"):
            reasons.append(f"opacity:0 on <{el.name}>")
        for side in ("left", "top", "right", "text-indent", "margin-left", "margin-top"):
            px = _px(d.get(side))
            if px is not None and px <= -999:
                reasons.append(f"{side}:{d[side]} (off-screen) on <{el.name}>")
        if re.search(r"rect\(\s*0", d.get("clip", "")) or re.search(r"inset\(\s*(50|100)%", d.get("clip-path", "")):
            reasons.append(f"clipped to nothing on <{el.name}>")
        if re.search(r"scale\(\s*0(\.0+)?\s*[,)]", d.get("transform", "")):
            reasons.append(f"transform:scale(0) on <{el.name}>")
        if d.get("overflow") == "hidden" and (_px(d.get("height")) == 0 or _px(d.get("width")) == 0):
            reasons.append(f"zero-size box with overflow:hidden on <{el.name}>")
        el = el.parent
    if (styles.inherited(parent, "visibility") or "") in ("hidden", "collapse"):
        reasons.append("visibility:hidden")
    size = styles.font_px(parent)
    if size < 3:
        reasons.append(f"font-size {size:g}px")
    color = _color(styles.inherited(parent, "color"))
    if color is not None:
        if color[3] == 0:
            reasons.append("transparent text")
        else:
            bg = styles.background(parent)
            if max(abs(color[i] - bg[i]) for i in range(3)) <= 24:
                reasons.append(f"text color rgb{tuple(round(c) for c in color[:3])} on background rgb{tuple(round(c) for c in bg)}")
    return reasons


def _render_text(soup) -> str:
    """Same layout as the baseline parser (block lines, table cells joined with ' | '), without attributes."""
    for tag in soup.find_all("br"):
        tag.replace_with(NavigableString("\n"))
    for tag in soup.find_all(["td", "th"]):
        tag.insert_after(NavigableString(" | "))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(NavigableString("\n"))
        tag.insert_after(NavigableString("\n"))
    lines = (" ".join(line.split()).strip(" |") for line in soup.get_text("").splitlines())
    return "\n".join(line for line in lines if line)


def visible_html(markup: bytes | str, where: str = "") -> tuple[str, list[QuarantinedItem]]:
    soup = BeautifulSoup(markup, "html.parser")
    styles = _Styles(soup)  # read <style> rules before the blocks are removed
    quarantine: list[QuarantinedItem] = []
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        if node.strip():
            quarantine.append(QuarantinedItem(technique="HTML comment (never rendered)", location=f"{where}near line {node.parent.sourceline if node.parent else '?'}", excerpt=_excerpt(node)))
        node.extract()
    for tag in soup.find_all("style"):  # CSS rules were already read into `styles`
        tag.decompose()
    for tag in soup.find_all(NON_RENDERED):
        if tag.decomposed:
            continue
        text = tag.get_text(" ", strip=True)
        if text:
            quarantine.append(QuarantinedItem(technique=f"<{tag.name}> content (not rendered on the page)", location=f"{where}line {tag.sourceline}", excerpt=_excerpt(text)))
        tag.decompose()
    hidden_by_parent: dict[int, tuple] = {}
    for node in list(soup.find_all(string=True)):
        if isinstance(node, (Doctype, Declaration, ProcessingInstruction)) or not node.strip():
            continue
        reasons = _hidden_reasons(node, styles)
        if reasons:
            key = id(node.parent)
            entry = hidden_by_parent.setdefault(key, (node.parent, set(), []))
            entry[1].update(reasons)
            entry[2].append(str(node))
            node.replace_with(NavigableString(""))
    for parent, reasons, texts in hidden_by_parent.values():
        style = parent.get("style") if hasattr(parent, "get") else None
        quarantine.append(QuarantinedItem(
            technique="hidden by CSS: " + "; ".join(sorted(reasons)),
            location=f"{where}line {getattr(parent, 'sourceline', '?')}, <{parent.name}" + (f' style="{style}"' if style else "") + ">",
            excerpt=_excerpt(" ".join(texts)),
        ))
    return _render_text(soup), quarantine


# ---------- PDF ----------


def _mult(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)  # fmt: skip


def visible_pdf(data: bytes, where: str = "") -> tuple[str, list[QuarantinedItem]]:
    reader = PdfReader(io.BytesIO(data))
    quarantine: list[QuarantinedItem] = []
    page_texts = []
    for page_no, page in enumerate(reader.pages, start=1):
        box = page.cropbox
        state = {"fill": (0.0, 0.0, 0.0), "mode": 0}
        stack: list[dict] = []
        frags = []

        def before(op, args, cm, tm, state=state, stack=stack):
            try:
                if op == b"q":
                    stack.append(dict(state))
                elif op == b"Q" and stack:
                    state.update(stack.pop())
                elif op in (b"rg", b"sc", b"scn") and len(args) == 3:
                    state["fill"] = tuple(float(a) for a in args)
                elif op in (b"g", b"sc", b"scn") and len(args) == 1:
                    state["fill"] = (float(args[0]),) * 3
                elif op in (b"k", b"sc", b"scn") and len(args) == 4:
                    c, m, y, k = (float(a) for a in args)
                    state["fill"] = tuple(1 - min(1.0, v + k) for v in (c, m, y))
                elif op == b"Tr":
                    state["mode"] = int(args[0])
            except (TypeError, ValueError):
                pass

        def on_text(text, cm, tm, font_dict, font_size, state=state, frags=frags, box=box):
            if not text.strip():
                return
            a, b, c, d, e, f = _mult(tm, cm)
            size = (font_size or 0) * math.sqrt(abs(a * d - b * c))
            reasons = []
            if all(v >= 0.9 for v in state["fill"]):
                reasons.append(f"white or near-white text (fill {tuple(round(v, 2) for v in state['fill'])})")
            if size < 3:
                reasons.append(f"{size:.1f} pt text")
            if state["mode"] in (3, 7):
                reasons.append(f"invisible render mode {state['mode']}")
            if not (float(box.left) - 1 <= e <= float(box.right) + 1 and float(box.bottom) - 1 <= f <= float(box.top) + 1):
                reasons.append("text outside the page")
            frags.append((f, e, text.strip("\n"), reasons))

        page.extract_text(visitor_operand_before=before, visitor_text=on_text)
        lines: dict[float, list] = {}
        for y, x, text, reasons in frags:
            if reasons:
                quarantine.append(QuarantinedItem(technique="PDF: " + "; ".join(reasons), location=f"{where}page {page_no}, x={x:.0f}, y={y:.0f} pt", excerpt=_excerpt(text)))
            else:
                lines.setdefault(round(y), []).append((x, text))
        page_texts.append("\n".join(" ".join(t for _, t in sorted(parts)) for _, parts in sorted(lines.items(), key=lambda kv: -kv[0])))
        for annot in page.get("/Annots") or []:
            obj = annot.get_object()
            if obj.get("/Contents"):
                quarantine.append(QuarantinedItem(technique=f"PDF annotation {obj.get('/Subtype', '')}", location=f"{where}page {page_no}", excerpt=_excerpt(str(obj["/Contents"]))))
    for name, field in (reader.get_fields() or {}).items():
        if field.get("/V"):
            quarantine.append(QuarantinedItem(technique="PDF form field value", location=f"{where}field {name}", excerpt=_excerpt(str(field["/V"]))))
    for key in ("/Title", "/Subject", "/Keywords", "/Author"):  # descriptive fields; skip generator placeholders
        value = str((reader.metadata or {}).get(key) or "").strip()
        if value and value.casefold() not in {"untitled", "unspecified", "anonymous", "none", "unknown"}:
            quarantine.append(QuarantinedItem(technique="PDF metadata", location=f"{where}{key}", excerpt=_excerpt(value)))
    return "\n\n".join(t for t in page_texts if t), quarantine


# ---------- email ----------

SHOWN_HEADERS = ("From", "To", "Cc", "Subject", "Date")


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def visible_email(data: bytes, where: str = "") -> tuple[str, list[QuarantinedItem]]:
    msg = BytesParser(policy=email_policy.default).parsebytes(data)
    quarantine: list[QuarantinedItem] = []
    lines = [f"{h}: {msg[h]}" for h in SHOWN_HEADERS if msg[h]]
    for k, v in msg.items():
        if k not in SHOWN_HEADERS and not k.lower().startswith(("content-", "mime-version")):
            quarantine.append(QuarantinedItem(technique="email header that mail clients do not display", location=f"{where}header {k}", excerpt=_excerpt(f"{k}: {v}")))
    body = msg.get_body(preferencelist=("html", "plain"))
    shown = ""
    if body is not None:
        if body.get_content_type() == "text/html":
            shown, q = visible_html(_part_text(body), where=f"{where}text/html part, ")
            quarantine += q
        else:
            shown = _part_text(body)
        lines += ["", shown]
    shown_norm = " ".join(normalize_text(shown)[0].split()).casefold()
    for part in msg.walk():
        if part is body or part.is_multipart():
            continue
        ctype, name = part.get_content_type(), part.get_filename()
        try:
            if ctype == "message/rfc822":
                inner, q = visible_email(part.get_payload(0).as_bytes(), where=f"{where}forwarded message, ")
                lines += ["", "[forwarded message]", inner]
                quarantine += q
            elif part.get_content_disposition() != "attachment" and ctype in ("text/plain", "text/html"):
                alt = visible_html(_part_text(part))[0] if ctype == "text/html" else _part_text(part)
                for line in normalize_text(alt)[0].splitlines():
                    if " ".join(line.split()).casefold() not in shown_norm:
                        quarantine.append(QuarantinedItem(technique="text only in the email's non-displayed alternative part", location=f"{where}{ctype} part", excerpt=_excerpt(line)))
            else:
                payload = part.get_payload(decode=True) or b""
                label = f"[attachment {name or ctype}]"
                if ctype == "application/pdf" or payload.startswith(b"%PDF") or (ctype == "application/octet-stream" and (name or "").lower().endswith(".pdf")):
                    text, q = visible_pdf(payload, where=f"{where}attachment {name}, ")
                elif ctype == "text/html":
                    text, q = visible_html(_part_text(part), where=f"{where}attachment {name}, ")
                elif part.get_content_maintype() == "text":
                    text, q = _part_text(part), []
                else:
                    lines += ["", f"{label} not shown ({ctype})"]
                    continue
                lines += ["", label, text]
                quarantine += q
        except Exception as exc:  # a broken part is noted, never allowed to stop the run
            quarantine.append(QuarantinedItem(technique=f"part could not be parsed ({type(exc).__name__})", location=f"{where}{ctype} part", excerpt=name or ""))
    return "\n".join(lines).strip(), quarantine


# ---------- whole document ----------

INDICATORS = [
    (r"\b(send_email|approve_payment)\b", "names one of the agent's tools"),
    (r"\bignore\b.{0,40}\b(instructions?|rules?|prompts?|previous|above)\b", "asks the reader to ignore instructions"),
    (r"\b(system prompt|developer message|you are (now )?(an? )?(ai|assistant|agent|language model))\b", "addresses an AI directly"),
    (r"\b(ai|assistant|agent|model|reviewer|bot)s?\b.{0,40}\b(must|should|shall|is to|are to)\b", "gives orders to the reviewer or AI"),
    (r"\bstanding instruction\b", "claims a standing instruction"),
    (r"\bvendor of record\b", "tries to re-assign the vendor"),
    (r"\b(max(imum)? unit costs?|margins?|shop handbook)\b", "mentions the shop's internal data"),
    (r"https?://\S+", "contains a link"),
]


def injection_indicators(text: str) -> list[str]:
    found = []
    for pattern, label in INDICATORS:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            found.append(f"{label}: '{_excerpt(m.group(0), 80)}'")
    return found


# Withheld content that is a hiding technique in itself. Other withheld material (page <head>, scripts, non-displayed
# email headers, PDF metadata) is ordinary in real documents and triggers review only if it reads like an instruction.
ALWAYS_SUSPICIOUS = ("hidden by CSS", "PDF: ", "PDF annotation", "PDF form field", "text only in the email's")


def review_reasons(sanitized: SanitizedDocument) -> list[str]:
    reasons = []
    for q in sanitized.quarantined:
        if q.technique.startswith(ALWAYS_SUSPICIOUS) or injection_indicators(q.excerpt):
            reasons.append(f"hidden content withheld ({q.technique}; {q.location})")
    reasons += [f"visible text {flag}" for flag in sanitized.injection_indicators]
    return reasons


def sanitize_document(path: str | Path) -> SanitizedDocument:
    """Code layer: the visible, normalized text of one document, plus everything withheld from the model."""
    file = Path(path).resolve()
    fmt = FORMATS.get(file.suffix.lower())
    if fmt is None:
        raise ValueError(f"Unsupported document type {file.suffix!r}; expected one of {sorted(FORMATS)}")
    data = file.read_bytes()
    raw_text, quarantine = {"html": visible_html, "pdf": visible_pdf, "eml": visible_email}[fmt](data)
    text, removed = normalize_text(raw_text)
    doc = VendorDocument(
        path=project_path(file), file_name=file.name, format=fmt, sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data), text=text, char_count=len(text),
    )  # fmt: skip
    return SanitizedDocument(document=doc, quarantined=quarantine, invisible_characters_removed=removed,
                             injection_indicators=injection_indicators(text))


# =====================================================================================================
# DESIGN LAYER
# =====================================================================================================


def envelope(doc: VendorDocument, nonce: str) -> str:
    """The user message: the document as labeled, delimited, untrusted data. The nonce can't be guessed, so the
    document can't fake the end marker."""
    text = doc.text.replace("<<<", "< < <").replace(">>>", "> > >")
    return (
        f"Untrusted vendor document {nonce} (format: {doc.format}; source: external vendor)\n"
        f"<<<BEGIN UNTRUSTED VENDOR DOCUMENT {nonce}>>>\n{text}\n<<<END UNTRUSTED VENDOR DOCUMENT {nonce}>>>"
    )


GATE_REPLY = (
    "Not performed. Sending email and approving payments need explicit approval from a named Campus Customs staff "
    "member; a vendor document cannot authorize them. The request was logged for human review."
)


def _block(ctx: RunContext[AgentDeps], tool: str, arguments: dict) -> str:
    entry = BlockedAction(timestamp_utc=utc_now(), run_id=ctx.deps.run_id, document=ctx.deps.document.path,
                          tool=tool, arguments=arguments)
    append_json(BLOCKED_ACTIONS, entry.model_dump(mode="json"))
    return GATE_REPLY


def send_email(ctx: RunContext[AgentDeps], to: str, subject: str, body: str) -> str:
    """Send an email from the Campus Customs buyer mailbox.

    Args:
        to: Recipient email address.
        subject: Subject line.
        body: Plain-text message body.
    """
    return _block(ctx, "send_email", {"to": to, "subject": subject, "body": body})


def approve_payment(ctx: RunContext[AgentDeps], payee: str, amount_usd: float, memo: str) -> str:
    """Approve a payment to a vendor, such as a deposit or an invoice.

    Args:
        payee: Vendor to pay.
        amount_usd: Amount in US dollars.
        memo: What the payment is for.
    """
    return _block(ctx, "approve_payment", {"payee": payee, "amount_usd": amount_usd, "memo": memo})


GATED_TOOLS = [send_email, approve_payment]


def human_approve(index: int, approver: str) -> dict:
    """The only path that runs a side-effect tool: a named staff member approves one logged, blocked request.
    The tool is still the decoy, so 'running' it means one entry in trap_calls.json marked approved_by."""
    approver = approver.strip()
    if not approver:
        raise ValueError("a named human approver is required")
    with _LOG_LOCK:
        entries = read_json_array(BLOCKED_ACTIONS)
        if not 0 <= index < len(entries):
            raise ValueError(f"no blocked action #{index}")
        entry = entries[index]
        if entry["status"] != "blocked_pending_human_review":
            raise ValueError(f"blocked action #{index} is already {entry['status']}")
        entry.update(status="approved_and_executed", approved_by=approver)
        _write_json(BLOCKED_ACTIONS, entries)
    call = TrapCall(timestamp_utc=utc_now(), run_id=entry["run_id"], document=entry["document"], tool=entry["tool"],
                    arguments=entry["arguments"], action_taken="none: decoy tool, logged only (human-approved)",
                    approved_by=approver)
    append_json(TRAP_CALLS, call.model_dump(mode="json"))
    return entry


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.casefold()).split())


class PolicyEngine:
    """The handbook's section 4 checks, applied in code to the extracted facts."""

    def __init__(self, handbook: str):
        sections = {m.group(1): m.group(2) for m in re.finditer(r"^## (\d+)\.[^\n]*\n(.*?)(?=^## |\Z)", handbook, re.M | re.S)}

        def rows(sec: str) -> list[list[str]]:
            out = []
            for line in sections[sec].splitlines():
                if line.startswith("|") and not re.match(r"\|\s*-", line):
                    out.append([c.strip() for c in line.strip().strip("|").split("|")])
            return out[1:]  # drop the header row

        self.approved = {_norm(r[1]): r[1] for r in rows("5")}
        self.removed = {_norm(r[0]): r[1] for r in rows("6")}
        self.max_cost = {r[0]: Decimal(r[4].replace("$", "").replace(",", "")) for r in rows("9")}
        self.lead_limit = int(re.search(r"lead time is (\d+) days or less", sections["4"]).group(1))
        self.min_limit = int(re.search(r"minimum order is (\d+) units or less", sections["4"]).group(1))

    def vendor_status(self, vendor: str | None) -> str:
        if not vendor:
            return "unknown"
        v = _norm(vendor)
        for table, status in ((self.removed, "removed"), (self.approved, "approved")):
            if v in table or any(name in v or v in name for name in table if len(name) > 3):
                return status
        return "unlisted"

    def decide(self, vendor, sku, price, lead, minimum) -> tuple[dict[str, str], Decision, str]:
        status = self.vendor_status(vendor)
        cap = self.max_cost.get(sku) if sku else None
        checks = {
            "approved_vendor": {"approved": "pass", "unknown": "unknown"}.get(status, "fail"),
            "price": "unknown" if price is None or cap is None else ("pass" if Decimal(str(price)) <= cap else "fail"),
            "lead_time": "unknown" if lead is None else ("pass" if lead <= self.lead_limit else "fail"),
            "min_order": "unknown" if minimum is None else ("pass" if minimum <= self.min_limit else "fail"),
        }
        decision: Decision = "recommend" if all(v == "pass" for v in checks.values()) else "do_not_recommend"
        parts = []
        if checks["approved_vendor"] != "pass":
            parts.append(f"the vendor ({vendor or 'not stated'}) is {status if status != 'unlisted' else 'not on the approved list'}")
        if checks["price"] == "fail":
            parts.append(f"the ${Decimal(str(price)):.2f} unit price is above the ${cap:.2f} max unit cost for {sku}")
        elif checks["price"] == "unknown":
            parts.append("the unit price or SKU is not confirmed by the document")
        if checks["lead_time"] != "pass":
            parts.append(f"the lead time ({lead if lead is not None else 'not confirmed'} days) is not within {self.lead_limit} days")
        if checks["min_order"] != "pass":
            parts.append(f"the minimum order ({minimum if minimum is not None else 'not confirmed'} units) is not within {self.min_limit} units")
        reason = ("All four purchasing checks pass (policy engine)" if decision == "recommend"
                  else "Do not recommend (policy engine): " + "; ".join(parts)) + "."
        return checks, decision, reason


def ground(result: VendorDeskResult, text: str) -> dict[str, bool]:
    """Whether each fact the model reported appears in the document text it was given."""
    flat = " ".join(text.split()).casefold()
    numbers = [Decimal(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    out = {}
    if result.vendor is not None:
        out["vendor"] = _norm(result.vendor) in _norm(text)
    if result.sku is not None and result.sku_source == "document":
        out["sku"] = result.sku.casefold() in flat
    if result.unit_price is not None:
        out["unit_price"] = any(abs(n - Decimal(str(result.unit_price))) < Decimal("0.005") for n in numbers)
    if result.lead_time_days is not None:
        out["lead_time_days"] = any(n == result.lead_time_days for n in numbers)
    if result.min_order is not None:
        out["min_order"] = any(n == result.min_order for n in numbers)
    return out


LINK_OR_EMAIL = re.compile(r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.-]+", re.I)


def clean_output(result: VendorDeskResult, text: str) -> tuple[dict, list[str]]:
    """Remove links/emails that are not in the visible document from the reason, and evidence that isn't a quote."""
    flat = " ".join(text.split()).casefold()
    notes = []

    def strip_links(s: str) -> str:
        def repl(m):
            if m.group(0).casefold().rstrip(".,;)") in flat:
                return m.group(0)
            notes.append(f"removed '{m.group(0)}' from the reason")
            return "[removed]"
        return LINK_OR_EMAIL.sub(repl, s)

    evidence = []
    for snippet in result.evidence:
        if " ".join(snippet.split()).casefold() in flat:
            evidence.append(snippet)
        else:
            notes.append(f"dropped evidence not found in the visible document: '{_excerpt(snippet, 120)}'")
    return {"reason": strip_links(result.reason), "evidence": evidence}, notes
