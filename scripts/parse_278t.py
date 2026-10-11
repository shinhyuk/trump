"""Parse OGE Form 278-T (Periodic Transaction Report) PDFs into transactions.

The President's 278-T filings are scanned documents with a noisy OCR text layer
(and occasionally no text layer at all), so parsing is position based: each
transaction row is anchored on its date cell, and the other cells are taken
from words in the same horizontal band, split by column x positions.
"""
import difflib
import os
import re
import statistics
import subprocess
import tempfile

import pymupdf

# Standard OGE value ranges (low, high). The last one has no upper bound.
BUCKETS = [
    (1_001, 15_000),
    (15_001, 50_000),
    (50_001, 100_000),
    (100_001, 250_000),
    (250_001, 500_000),
    (500_001, 1_000_000),
    (1_000_001, 5_000_000),
    (5_000_001, 25_000_000),
    (25_000_001, 50_000_000),
    (50_000_001, None),
]
_BUCKET_KEYS = [f"{lo}{hi or ''}" for lo, hi in BUCKETS]

_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1",
                            "i": "1", "|": "1", "!": "1", "S": "5", "s": "5", "B": "8",
                            "Z": "2", "z": "2", "G": "6", "b": "6", "g": "9", "q": "9"})
_DATE_RE = re.compile(r"^(\d{1,2})[/\\](\d{1,2})(?:[/\\]?(\d{4})|/(\d{2}))$")


def _norm_date(token, filing_year, strict=True):
    """Parse an OCR'd m/d/yyyy token. With strict=False, also accept the common
    garbles ("8l10h026", "8/1012026", "813/2026") seen inside the date column."""
    t = token.strip(".,;:'\"`()[]")
    m = _DATE_RE.match(t.translate(_DIGIT_FIX))
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
    elif strict:
        return None
    else:
        t = re.sub(r"[\\'|]", "/", t)
        if len(t) < 6:
            return None
        body = t[:-4].rstrip("/")
        parts = [p.translate(_DIGIT_FIX) for p in re.split(r"[/lhIi]", body) if p]
        parts = [re.sub(r"\D", "", p) for p in parts]
        parts = [p for p in parts if p]
        if len(parts) >= 2:
            mo_s, dy_s = parts[0], parts[1]
        elif len(parts) == 1 and len(parts[0]) >= 2:
            mo_s, dy_s = parts[0][0], parts[0][1:]
        else:
            return None
        if len(dy_s) == 3 and dy_s.endswith("1"):  # slash before the year read as "1"
            dy_s = dy_s[:2]
        try:
            mo, dy = int(mo_s), int(dy_s[:2])
        except ValueError:
            return None
        if mo > 12 and len(mo_s) == 2:
            mo = int(mo_s[0])
    if not (1 <= mo <= 12 and 1 <= dy <= 31):
        return None
    # Year digits are the least reliable (6 is often read as 8), and a transaction
    # always falls in the year before the filing or the filing year itself.
    yr = filing_year or 2026
    try:
        m_yr = int(t.translate(_DIGIT_FIX)[-4:])
        if yr - 1 <= m_yr <= yr:
            yr = m_yr
    except ValueError:
        pass
    return f"{yr:04d}-{mo:02d}-{dy:02d}"


def _parse_amount(text):
    # Keep only the money tokens (drop the "Yes"/"No" late-notification cell),
    # and treat an S right before a digit as a misread "$".
    toks = [t for t in text.split() if any(c.isdigit() for c in t) or t.lower().startswith("over")]
    t = re.sub(r"[S$](?=[\dOolI])", " ", " ".join(toks))
    if "over" in t.lower() and "50" in t:
        return BUCKETS[-1]
    digits = re.sub(r"\D", "", t.translate(_DIGIT_FIX))
    if not digits:
        return None
    # "Over $50,000,000" is only taken when the word "Over" is legible.
    best = max(range(len(BUCKETS) - 1),
               key=lambda i: difflib.SequenceMatcher(None, digits, _BUCKET_KEYS[i]).ratio())
    return BUCKETS[best]


def _parse_type(text):
    """Classify the OCR'd "Type" cell. "Purchase" and "Sale" are garbled in many
    ways ("oun:haao", "aalo", "1810"), so lean on shape as well as letters."""
    t = re.sub(r"[^a-z0-9:]", "", text.lower()).lstrip("l1i")
    if not t:
        return None
    if "xch" in t or t.startswith("exc"):
        return "exchange"
    if any(k in t for k in ("n:h", "rch", "urc", "chas", "rcha")) or t[:2] in ("du", "ou", "pu", "cu", "dn"):
        return "purchase"
    if "sal" in t or "ale" in t or "alo" in t or len(t) <= 5:
        return "sale"
    return "purchase"


def _page_words(page):
    words = page.get_text("words")
    if sum(len(w[4]) for w in words) >= 200:
        return words
    return _ocr_words(page)


def _ocr_words(page, dpi=300):
    """Image-only page: OCR with the tesseract CLI (automatic page segmentation
    copes with the ruled table far better than PyMuPDF's built-in OCR call)."""
    scale = 72 / dpi
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "p.png")
        page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY).save(png)
        tsv = subprocess.run(["tesseract", png, "-", "-l", "eng", "tsv"],
                             capture_output=True, text=True, check=True).stdout
    words = []
    for line in tsv.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or not f[11].strip() or float(f[10]) < 0:
            continue
        x, y, w, h = (int(v) * scale for v in f[6:10])
        words.append((x, y, x + w, y + h, f[11]))
    return words


def parse_pdf(path, filing_year=None):
    doc = pymupdf.open(path)
    pages = [[w for w in _page_words(doc[p]) if w[4].strip()] for p in range(doc.page_count)]
    widths = [doc[p].rect.width for p in range(doc.page_count)]
    strict_x = [x for ws, wd in zip(pages, widths) for x in _strict_date_xs(ws, filing_year, wd)]
    doc_date_x = _date_column(strict_x)
    rows = []
    for pno, words in enumerate(pages):
        rows.extend(_parse_page(words, filing_year, pno, doc_date_x, widths[pno]))
    return rows


def _strict_date_xs(words, filing_year, width):
    # Bond descriptions contain dates too ("DUE 11/15/41"); the Date column sits
    # in the right half of the form.
    return [w[0] for w in words if w[0] > 0.45 * width and _norm_date(w[4], filing_year)]


def _date_column(xs):
    """x of the densest 12pt-wide cluster of date tokens."""
    if not xs:
        return None
    best = max(xs, key=lambda x: sum(1 for y in xs if abs(y - x) <= 6))
    return statistics.median([y for y in xs if abs(y - best) <= 12])


def _parse_page(words, filing_year, pno, doc_date_x, width):
    strict_xs = _strict_date_xs(words, filing_year, width)
    if len(strict_xs) >= 3 or (strict_xs and doc_date_x is None):
        date_x0 = _date_column(strict_xs)
    elif doc_date_x is not None:
        date_x0 = doc_date_x
    else:
        return []
    anchors = []
    for w in words:
        # Anything date-shaped in the date column starts a row, even when the
        # OCR is too garbled to recover the date itself.
        if abs(w[0] - date_x0) > 12 or len(w[4]) < 5 or not any(c.isdigit() for c in w[4]):
            continue
        anchors.append((w, _norm_date(w[4], filing_year)
                        or _norm_date(w[4], filing_year, strict=False)))
    if not anchors:
        return []
    date_x0 = statistics.median(a[0][0] for a in anchors)
    date_x1 = statistics.median(a[0][2] for a in anchors)
    anchors.sort(key=lambda a: a[0][1])

    def same_line(w, y0, y1):
        cy = (w[1] + w[3]) / 2
        return y0 - 2 <= cy <= y1 + 2

    # Column guesses from the anchor lines.
    type_xs, desc_xs = [], []
    for w, _ in anchors:
        line = sorted((v for v in words if same_line(v, w[1], w[3]) and v[2] <= w[0] + 1),
                      key=lambda v: v[0])
        line = [v for v in line if v is not w]
        if line and _parse_type(line[-1][4]):
            type_xs.append(line[-1][0])
        if len(line) >= 2:
            desc_xs.append(line[1][0])
    type_x = statistics.median(type_xs) if type_xs else date_x0 - 85
    desc_x = statistics.median(desc_xs) if desc_xs else None

    out = []
    for i, (aw, date) in enumerate(anchors):
        top = aw[1] - 3
        nxt = anchors[i + 1][0][1] - 3 if i + 1 < len(anchors) else aw[3] + 40
        bottom = min(nxt, aw[3] + 45)
        band = [w for w in words if top <= (w[1] + w[3]) / 2 < bottom]
        line = [w for w in band if same_line(w, aw[1], aw[3])]

        type_words = [w for w in band if type_x - 15 <= w[0] < date_x0 - 3 and w[1] < aw[3] + 12]
        ttype = _parse_type(" ".join(w[4] for w in sorted(type_words, key=lambda w: w[0])))
        # Amounts sometimes wrap onto a second line inside the row.
        amt_words = sorted((w for w in band if w[0] > date_x1 + 2),
                           key=lambda w: (not same_line(w, aw[1], aw[3]), w[0]))
        amt_text = " ".join(w[4] for w in amt_words)
        amount = _parse_amount(amt_text)

        left = sorted((w for w in band if w[2] <= type_x - 2 or (w[0] < type_x - 15)),
                      key=lambda w: (round(w[1] / 6), w[0]))
        left = [w for w in left if w[0] < type_x - 15]
        num = None
        desc_words = []
        for w in left:
            if desc_x is not None and w[2] < desc_x - 5:
                if re.fullmatch(r"\d{1,4}", w[4]) and same_line(w, aw[1], aw[3]):
                    num = int(w[4])
                continue
            desc_words.append(w[4])
        desc = re.sub(r"\s+", " ", " ".join(desc_words)).strip()
        if not desc or amount is None:
            continue
        out.append({
            "n": num,
            "page": pno + 1,
            "description": desc,
            "type": ttype or "purchase",
            "type_uncertain": ttype is None,
            "date": date,
            "amount_low": amount[0],
            "amount_high": amount[1],
        })
    return out


if __name__ == "__main__":
    import json
    import sys
    rows = parse_pdf(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
    json.dump(rows, sys.stdout, indent=1)
    nums = [r["n"] for r in rows if r["n"]]
    print(f"\nrows={len(rows)} max_n={max(nums) if nums else None}", file=sys.stderr)
