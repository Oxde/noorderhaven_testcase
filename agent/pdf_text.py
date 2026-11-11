"""Standard-library text extraction for the ReportLab PDFs in the claims pack.

Handles content streams with `/Filter [/ASCII85Decode /FlateDecode]` or `/FlateDecode`, and the text
operators `Tj`, `TJ`, `'` and `"`. Emits one line per `Td`/`TD`/`T*`/`ET`. Not a general PDF parser:
it raises `PdfTextError` with context when a stream cannot be decoded.
"""
from __future__ import annotations

import base64
import re
import zlib
from pathlib import Path
from typing import List

_STREAM_RE = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\n?endstream", re.S)
# Tokens in a content stream: literal strings, arrays, names, numbers, operators.
_TOKEN_RE = re.compile(rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)|\[|\]|/[^\s/\[\]()<>]+|[-+]?\d*\.?\d+|[A-Za-z'\"*]+")


class PdfTextError(Exception):
    pass


def _decode_stream(header: bytes, data: bytes, path: Path) -> bytes:
    # Apply filters in the order written in the /Filter entry.
    m = re.search(rb"/Filter\s*(\[[^\]]*\]|/\w+)", header)
    filters = re.findall(rb"/(\w+)", m.group(1)) if m else []
    out = data
    for f in filters:
        try:
            if f in (b"ASCII85Decode", b"A85"):
                s = out.strip()
                if s.startswith(b"<~"):
                    s = s[2:]
                if not s.endswith(b"~>"):
                    s = s + b"~>"
                out = base64.a85decode(b"<~" + s, adobe=True)
            elif f in (b"FlateDecode", b"Fl"):
                out = zlib.decompress(out)
            else:
                raise PdfTextError("unsupported filter %r in %s" % (f, path))
        except (ValueError, zlib.error) as e:
            raise PdfTextError("cannot decode stream (filter %r) in %s: %s" % (f, path, e)) from e
    return out


def _unescape(lit: bytes) -> str:
    """Decode a PDF literal string body (without the outer parentheses)."""
    out = bytearray()
    i = 0
    n = len(lit)
    while i < n:
        c = lit[i]
        if c == 0x5C and i + 1 < n:  # backslash
            nxt = lit[i + 1]
            esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
                   ord("("): 40, ord(")"): 41, ord("\\"): 92}
            if nxt in esc:
                out.append(esc[nxt])
                i += 2
            elif 0x30 <= nxt <= 0x37:
                j = i + 1
                digits = b""
                while j < n and len(digits) < 3 and 0x30 <= lit[j] <= 0x37:
                    digits += bytes([lit[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
            elif nxt in (10, 13):  # line continuation
                i += 2
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(c)
            i += 1
    return out.decode("cp1252", errors="replace")


def _content_text(content: bytes) -> List[str]:
    lines: List[str] = []
    cur: List[str] = []
    stack: List[object] = []
    in_array = False
    array: List[str] = []

    def flush() -> None:
        if cur:
            line = "".join(cur).strip()
            if line:
                lines.append(line)
            cur.clear()

    for m in _TOKEN_RE.finditer(content):
        tok = m.group(0)
        if tok.startswith(b"("):
            s = _unescape(tok[1:-1])
            (array if in_array else stack).append(s)
        elif tok == b"[":
            in_array, array = True, []
        elif tok == b"]":
            in_array = False
            stack.append(list(array))
        elif in_array:
            # Kerning numbers inside TJ: a large negative gap is a word space.
            try:
                if float(tok) < -200:
                    array.append(" ")
            except ValueError:
                pass
        elif tok in (b"Tj",):
            if stack and isinstance(stack[-1], str):
                cur.append(stack[-1])
            stack.clear()
        elif tok == b"TJ":
            if stack and isinstance(stack[-1], list):
                cur.append("".join(stack[-1]))
            stack.clear()
        elif tok in (b"'", b'"'):
            flush()
            if stack and isinstance(stack[-1], str):
                cur.append(stack[-1])
            stack.clear()
        elif tok in (b"Td", b"TD", b"T*", b"ET", b"Tm"):
            flush()
            stack.clear()
        elif re.fullmatch(rb"[A-Za-z*]+", tok):
            stack.clear()
        else:
            stack.append(tok)
    flush()
    return lines


def extract_text(path: Path) -> str:
    """Return the text of a ReportLab PDF, one line per text line, pages in file order."""
    path = Path(path)
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF"):
        raise PdfTextError("not a PDF: %s" % path)
    lines: List[str] = []
    for m in _STREAM_RE.finditer(raw):
        header, data = m.group(1), m.group(2)
        if b"/Subtype /Image" in header or b"/Subtype/Image" in header:
            continue
        content = _decode_stream(header, data, path)
        if b"BT" not in content:
            continue
        lines.extend(_content_text(content))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print("=== %s" % p)
        print(extract_text(Path(p)))
