#!/usr/bin/env python3
"""Read each declarant's full name from an AMF dealing disclosure by its position.

In the AMF tables the declarant ("Opérateur" / "Déclarant") sits in one merged cell
that spans all of that declarant's dealing rows, with the name centred vertically and
often wrapped over two or three lines. Read line by line (parse-filings.py), the name
gets cut short ("Management LP", "Pictet Asset", "et consignations") or lost altogether
(rows with a blank filer) whenever the name lines and the dealing lines interleave.

Here the page is read with text positions instead:
  1. the lines in the Opérateur column are grouped into one name per cell (lines a
     normal line-spacing apart belong together; a footnote star "*" ends a name);
  2. every other line of the table is a "row"; the rows are split into consecutive
     blocks, one per name, so that each name sits at the vertical centre of its block;
  3. each dated dealing row takes the name of its block.

relabel_rows() then puts those names on the rows parse-filings.py extracted. It only
replaces a filer that is blank, an obvious fragment, or part of the full name, so a
name the old reader got right is never swapped for a different one (those cases are
counted as conflicts for review instead).

Used by parse-filings.py (every new notice) and repair_declarant_names.py (one-off
repair of the stored notices).
"""
import re
import unicodedata
from collections import defaultdict

DATED_RE = re.compile(r"\b(?:le|du)\s+(\d{2})/(\d{2})/(\d{4})\b", re.I)
CODE_RE = re.compile(r"^code\s+[A-Z]{2}[A-Z0-9]{10}\b", re.I)
HEADER_RE = re.compile(r"^(op[ée]rateur|d[ée]clarant)\b", re.I)
SEPARATOR_RE = re.compile(r"^_{3,}$")
NUMERIC_RE = re.compile(r"^[\d\s.,*()+\-–— /%€]+$")
AMF_REF_RE = re.compile(r"\d{3}C\d{4}")

LINE_TOLERANCE = 2.0     # pt: characters this close vertically are on one line
NAME_LINE_GAP = 13.0     # pt: name lines further apart than this are different cells
HEADER_TO_NAME_MAX = 45  # pt: the name column ends this far right of the header label
CONNECTORS = {"ET", "DE", "DES", "DU", "D", "LA", "LE", "LES", "AND", "OF", "THE", "&"}


# --------------------------------------------------------------------------- reading

def _iter_chars(container):
    from pdfminer.layout import LTChar, LTContainer

    for obj in container:
        if isinstance(obj, LTChar):
            yield obj
        elif isinstance(obj, LTContainer):
            yield from _iter_chars(obj)


def _baseline(char):
    matrix = getattr(char, "matrix", None)
    if matrix and abs(matrix[1]) < 1e-6 and abs(matrix[2]) < 1e-6:
        return float(matrix[5])
    return float(char.y0)


def page_segments(page_layout):
    """Pieces of text on a page as (x, y, text): characters on one baseline, split
    wherever there is a column-sized gap between them."""
    chars = [c for c in _iter_chars(page_layout) if getattr(c, "upright", True)]
    chars.sort(key=lambda c: (-_baseline(c), c.x0))
    lines = []
    for char in chars:
        y = _baseline(char)
        if lines and abs(lines[-1][0] - y) <= LINE_TOLERANCE:
            lines[-1][1].append(char)
        else:
            lines.append((y, [char]))

    segments = []
    for y, line_chars in lines:
        line_chars.sort(key=lambda c: c.x0)
        current = None
        for char in line_chars:
            text = char.get_text()
            size = max(float(char.size or 0), 4.0)
            if current is not None:
                gap = char.x0 - current["x1"]
                if gap > max(0.6 * size, 3.5):
                    segments.append(current)
                    current = None
                elif gap > 0.15 * size and not current["text"].endswith(" ") and text != " ":
                    current["text"] += " "
            if current is None:
                if not text.strip():
                    continue
                current = {"x": float(char.x0), "y": y, "x1": float(char.x1), "text": ""}
            current["text"] += text
            current["x1"] = max(current["x1"], float(char.x1))
        if current is not None:
            segments.append(current)

    out = []
    for seg in segments:
        text = re.sub(r"\s+", " ", seg["text"]).strip()
        if text:
            out.append({"x": seg["x"], "y": seg["y"], "text": text})
    return out


def _group_lines(items):
    lines = []
    for item in sorted(items, key=lambda i: (-i["y"], i["x"])):
        line = next((l for l in lines if abs(l["y"] - item["y"]) <= 1.5), None)
        if line is None:
            line = {"y": item["y"], "items": []}
            lines.append(line)
        line["items"].append(item)
    for line in lines:
        line["items"].sort(key=lambda i: i["x"])
        line["text"] = " ".join(i["text"] for i in line["items"])
    lines.sort(key=lambda l: -l["y"])
    return lines


def tidy_name(text):
    text = str(text or "").replace("*", " ")
    text = re.sub(r"(\w)-\s+(\w)", r"\1-\2", text)        # "Jean- Denis" -> "Jean-Denis"
    text = re.sub(r"&(?=\w)", "& ", text)                    # "Chase &Co." -> "Chase & Co."
    text = re.sub(r"(?<=[a-zà-ÿ])\d+$", "", text)  # footnote number: "Management1"
    return re.sub(r"\s+", " ", text).strip(" ,;")


def _name_blocks(name_lines):
    blocks = []
    for line in name_lines:
        prev = blocks[-1] if blocks else None
        prev_line = prev["lines"][-1] if prev else None
        if (
            prev is not None
            and prev_line["y"] - line["y"] <= NAME_LINE_GAP
            and not prev_line["text"].rstrip().endswith("*")
        ):
            prev["lines"].append(line)
        else:
            blocks.append({"lines": [line]})
    for block in blocks:
        block["name"] = tidy_name(" ".join(l["text"] for l in block["lines"]))
        block["centre"] = (block["lines"][0]["y"] + block["lines"][-1]["y"]) / 2
    return [b for b in blocks if re.search(r"[A-Za-z]", b["name"])]


def _assign_blocks(rows, blocks):
    """Split rows (top to bottom) into len(blocks) consecutive groups so that each
    group's vertical centre is as close as possible to its block's name."""
    n, k = len(rows), len(blocks)
    if k == 0 or n == 0:
        return [None] * n
    if k == 1:
        return [blocks[0]["name"]] * n
    if k > n:
        return [None] * n
    inf = float("inf")
    best = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[-1] * (n + 1) for _ in range(k + 1)]
    best[0][0] = 0.0
    for j in range(1, k + 1):
        centre = blocks[j - 1]["centre"]
        for end in range(j, n + 1):
            for start in range(j - 1, end):
                if best[j - 1][start] == inf:
                    continue
                cost = best[j - 1][start] + abs((rows[start]["y"] + rows[end - 1]["y"]) / 2 - centre)
                if cost < best[j][end]:
                    best[j][end] = cost
                    back[j][end] = start
    if best[k][n] == inf:
        return [None] * n
    names = [None] * n
    end = n
    for j in range(k, 0, -1):
        start = back[j][end]
        for idx in range(start, end):
            names[idx] = blocks[j - 1]["name"]
        end = start
    return names


def operations_from_pages(pages):
    """pages: list of segment lists (one per page, from page_segments).
    Returns the dated dealing rows in reading order: {page, y, text, date, filer}."""
    operations = []
    header_x = None
    last_name = None
    for page_no, segments in enumerate(pages, start=1):
        header = next((s for s in segments if HEADER_RE.match(s["text"])), None)
        if header is not None:
            header_x = header["x"]
        if header_x is None:
            continue  # the table has not started yet
        top = header["y"] - 3 if header is not None else float("inf")
        below = [s for s in segments if s["y"] < top]
        anchors = [s for s in below if DATED_RE.search(s["text"]) or CODE_RE.match(s["text"])]
        if not anchors:
            continue
        lowest = min(s["y"] for s in anchors)
        bottom = lowest - 12        # last table row
        name_bottom = lowest - 22   # the last name can hang a line or two lower
        footnotes = [s["y"] for s in below if s["text"].startswith("*") and s["y"] < lowest]
        if footnotes:
            name_bottom = max(name_bottom, max(footnotes))
        separators = [s["y"] for s in below if SEPARATOR_RE.match(s["text"]) and s["y"] > name_bottom]
        if separators:
            bottom = max(bottom, max(separators))
            name_bottom = max(name_bottom, max(separators))
        top_anchor = max(s["y"] for s in anchors)
        # Name lines can sit a little above the first dealing row (a centred 2-3 line
        # name). On a continuation page (no header) keep that margin small, so the
        # page heading above the table is not read as a name.
        margin = 20 if header is not None else 12
        table = [s for s in below if name_bottom < s["y"] <= top_anchor + margin]
        dated_x = [s["x"] for s in table if DATED_RE.search(s["text"])]
        if not dated_x:
            continue
        name_max_x = min(min(dated_x) - 1, header_x + HEADER_TO_NAME_MAX)

        def is_name(seg):
            text = seg["text"]
            return (
                seg["x"] < name_max_x
                and not text.startswith("*")
                and not NUMERIC_RE.match(text)
                and not DATED_RE.search(text)
                and not HEADER_RE.match(text)
                and not AMF_REF_RE.search(text)
            )

        name_lines = _group_lines([s for s in table if is_name(s)])
        rows = _group_lines([
            s for s in table if not is_name(s) and bottom < s["y"] <= top_anchor + 1
        ])
        blocks = _name_blocks(name_lines)
        if blocks:
            names = _assign_blocks(rows, blocks)
        else:
            names = [last_name] * len(rows)  # a continuation page: same declarant
        for row, name in zip(rows, names):
            match = DATED_RE.search(row["text"])
            if not match:
                continue
            day, month, year = match.groups()
            operations.append({
                "page": page_no,
                "y": round(row["y"], 1),
                "text": row["text"],
                "date": f"{year}-{month}-{day}",
                "filer": name,
            })
        if blocks:
            last_name = blocks[-1]["name"]
    return operations


def read_pdf(path, debug=False):
    """Dated dealing rows of one AMF notice with the declarant read by position.
    With debug=True, also returns the text pieces found on each page."""
    from pdfminer.high_level import extract_pages

    pages = [page_segments(page) for page in extract_pages(str(path), laparams=None)]
    operations = operations_from_pages(pages)
    return (operations, pages) if debug else operations


# --------------------------------------------------------------------------- relabelling

def _fold(name):
    text = unicodedata.normalize("NFD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).upper()
    text = text.replace("&", " & ").replace("*", " ")
    tokens = re.findall(r"[A-Z0-9&]+", text)
    if tokens:
        tokens[-1] = re.sub(r"(?<=[A-Z])\d+$", "", tokens[-1]) or tokens[-1]
    return [t for t in tokens if t]


def _contiguous_part(part, whole):
    n = len(part)
    return any(whole[i:i + n] == part for i in range(len(whole) - n + 1))


def is_junk(name):
    """A filer that cannot be a real name: blank, a number, an AMF reference, or a
    piece of a sentence ("et de", "et consignations")."""
    text = str(name or "").strip()
    if not text or not re.search(r"[A-Za-z]", text):
        return True
    if AMF_REF_RE.search(text):
        return True
    if text[0].islower():
        return True
    tokens = _fold(text)
    return bool(tokens) and all(t in CONNECTORS for t in tokens)


def can_replace(old, new):
    """True when `new` is the full form of `old` (or `old` is junk)."""
    if not new or is_junk(new):
        return False
    if is_junk(old):
        return True
    old_tokens, new_tokens = _fold(old), _fold(new)
    return bool(old_tokens) and (
        _contiguous_part(old_tokens, new_tokens) or _in_order(old_tokens, new_tokens)
    )


def _in_order(part, whole):
    # "TFG Asset LLP" in "TFG Asset Management UK LLP" (a middle line was lost);
    # the first word must match so a lone legal form never qualifies.
    if len(part) < 2 or part[0] != whole[0]:
        return False
    rest = iter(whole)
    return all(token in rest for token in part)


def _op_key(text):
    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("’", "'").replace("*", " ")
    match = DATED_RE.search(text)
    if match:
        text = text[: match.end()]
    return re.sub(r"[^a-z0-9/']+", " ", text).strip()


def _numbers(text):
    """Numbers on a line, with thousands spaces removed: "3 000 actions 44,58" ->
    {"3000", "44,58"}."""
    found = re.findall(r"\d{1,3}(?:[\s\u00a0\u202f]\d{3})+(?!\d)|\d+(?:,\d+)?", str(text or ""))
    return {re.sub(r"[\s\u00a0\u202f]", "", value) for value in found}


def match_names(rows, operations):
    """For each parsed row of a notice with several declarants, the declarant name of
    the positional dealing row it corresponds to (or None when that is not certain).

    Rows are matched on date and operation wording. Where several dealing rows share
    that wording, the name is used only if they all have the same declarant, or if the
    parser found exactly as many such rows as there are on the page (then in order)."""
    candidates = []
    for row in rows:
        date = row.get("transaction_date")
        row_key = _op_key(row.get("operation"))
        same_date = [i for i, op in enumerate(operations) if op["date"] == date]
        by_text = [
            i for i in same_date
            if row_key and (row_key in _op_key(operations[i]["text"]) or _op_key(operations[i]["text"]) in row_key)
        ]
        cands = by_text or same_date
        quantity = row.get("quantity")
        price = str(row.get("price_eur") or "").strip()
        for wanted in (
            str(abs(quantity)) if isinstance(quantity, int) and quantity else "",
            price if re.match(r"^\d+(?:,\d+)?$", price) else "",
        ):
            if wanted:
                narrowed = [i for i in cands if wanted in _numbers(operations[i]["text"])]
                if narrowed:
                    cands = narrowed
        candidates.append(tuple(cands))

    groups = defaultdict(list)
    for index, cands in enumerate(candidates):
        groups[cands].append(index)

    names = [None] * len(rows)
    for cands, row_indexes in groups.items():
        if not cands:
            continue
        filers = [operations[i].get("filer") for i in cands]
        if all(filers) and len(set(filers)) == 1:
            for index in row_indexes:
                names[index] = filers[0]
        elif len(row_indexes) == len(cands):
            for index, op_index in zip(row_indexes, cands):
                names[index] = operations[op_index].get("filer")
    return names


def relabel_rows(rows, operations, replace_conflicts=False):
    """Put the positional declarant names on the parsed rows of one notice.
    Changes rows in place; returns {"changed": [(old, new), ...], "conflicts": [...]}.
    A conflict (the row already has a different full name) is left alone unless
    replace_conflicts is set - only after the conflicts have been checked by hand."""
    stats = {"changed": [], "conflicts": [], "unmatched": 0}
    names = []
    for op in operations:
        if op.get("filer") and op["filer"] not in names:
            names.append(op["filer"])
    if not names or not rows:
        return stats

    if len(names) == 1 and all(op.get("filer") for op in operations):
        targets = [names[0]] * len(rows)
    else:
        targets = match_names(rows, operations)

    for row, new in zip(rows, targets):
        old = row.get("filer") or ""
        if not new:
            stats["unmatched"] += 1
            continue
        if old == new:
            continue
        if can_replace(old, new):
            if "filer_as_parsed" not in row:
                row["filer_as_parsed"] = old
            row["filer"] = new
            stats["changed"].append((old, new))
        else:
            stats["conflicts"].append((old, new))
            stats.setdefault("conflict_rows", []).append({
                "date": row.get("transaction_date"), "operation": row.get("operation"),
                "quantity": row.get("quantity"), "price": row.get("price_eur"),
                "old": old, "new": new,
            })
            if replace_conflicts:
                if "filer_as_parsed" not in row:
                    row["filer_as_parsed"] = old
                row["filer"] = new
    return stats


if __name__ == "__main__":
    import json
    import sys

    for pdf in sys.argv[1:]:
        print(pdf)
        for op in read_pdf(pdf):
            print("  ", json.dumps(op, ensure_ascii=False))
