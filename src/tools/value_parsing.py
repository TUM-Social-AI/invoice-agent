"""
Deterministic parsing of amounts and dates as printed on invoices.

One parser for every caller (extraction merge, compliance rules, grounding, evaluation),
so a value read as "425 000 FCFA" or "50.000 R" means the same number everywhere.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date
from typing import Any, Optional

# A grouped amount: a 1-3 digit head (not "0") followed by groups of exactly three digits, all
# with the same separator ("425 000", "50.000", "3,758", "2.366.181"), then optional decimals.
# Leading "0" is excluded so "0.125" stays a decimal.
_GROUPED_RE = re.compile(
    r"(?<![\d.,])[1-9]\d{0,2}(?P<sep>[ .,'\u2019\u202f])\d{3}(?:(?P=sep)\d{3})*(?![\d])"
    r"(?:(?P<dec>[.,])(?P<frac>\d+))?"
)
_PLAIN_RE = re.compile(r"\d+(?:[.,]\d+)?")
# Text allowed around a number for it to count as a pure amount (strict mode): currency codes,
# symbols and the handwritten "F"/"R" suffixes used on Chadian vouchers.
_AMOUNT_NOISE_RE = re.compile(
    r"(?i)\b(?:f\s?cfa|cfa|xaf|xof|eur|euros?|usd|dollars?|francs?|f|r)\b|[€$£%]"
)


def _clean(text: str) -> str:
    s = re.sub(r"<[^>]+>", "", text)
    s = s.replace("&nbsp;", " ").replace("&#160;", " ")
    s = s.replace("\xa0", " ").replace("\u2009", " ").replace("\u202f", " ")
    return s.strip()


def parse_amount(value: Any, *, strict: bool = False) -> Optional[float]:
    """
    Parse a printed amount into a float, or None when there is no number.

    - groups of three digits separated by a space, dot, comma or apostrophe are thousands:
      "425 000" -> 425000, "50.000" -> 50000, "3,758.00" -> 3758.0, "1.234,56" -> 1234.56
    - a lone comma or dot not followed by exactly three digits is the decimal mark: "12,5" -> 12.5
    - currency marks and units are ignored: "$3,758.00", "30.000 R", "425 000 FCFA", "19%"
    - "(120)" and "-120" are negative

    strict=True returns None unless the text is only an amount plus currency marks, so
    references such as "006/09/2025" or "N° 0007932 du 31/07" are not read as numbers.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f

    s = _clean(str(value))
    if not s or s.lower() in ("null", "none"):
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()

    grouped = _GROUPED_RE.search(s)
    plain = _PLAIN_RE.search(s)
    if grouped and (plain is None or grouped.start() <= plain.start()):
        m = grouped
        whole =re.sub(r"[ .,'\u2019\u202f]", "", s[m.start(): m.start("dec") if m.group("dec") else m.end()])
        frac = m.group("frac") or ""
        number = f"{whole}.{frac}" if frac else whole
    elif plain:
        m = plain
        number = m.group(0).replace(",", ".")
    else:
        return None

    # A minus sign directly in front of the number ("-120", "€ -120"), not a dash between words.
    if re.search(r"(?:^|[\s(€$£])-$", s[: m.start()]):
        negative = True
    if strict:
        rest = (s[: m.start()] + " " + s[m.end():])
        rest = _AMOUNT_NOISE_RE.sub(" ", rest)
        if re.sub(r"[\s:\-+=*.,]", "", rest):
            return None

    try:
        v = float(number)
    except ValueError:
        return None
    return -v if negative else v


# --- Dates ---------------------------------------------------------------------------------

_MONTHS = {
    # French
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    # Spanish
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
# Abbreviations ("janv", "févr", "sept", "dec", "Jul") resolved by unique prefix of >= 3 letters.
_MONTH_WORD_RE = re.compile(r"[a-z]{3,}")


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _month_from_word(word: str) -> Optional[int]:
    if word in _MONTHS:
        return _MONTHS[word]
    hits = {n for name, n in _MONTHS.items() if name.startswith(word)}
    return hits.pop() if len(hits) == 1 else None


def _year(token: str) -> Optional[int]:
    y = int(token)
    if len(token) == 2:
        return 2000 + y
    if len(token) == 4 and 1900 <= y <= 2100:
        return y
    return None


def _iso(y: Optional[int], m: Optional[int], d: int) -> Optional[str]:
    if y is None or m is None:
        return None
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_full_date(value: Any, order: str = "DMY") -> Optional[str]:
    """
    Return the date as YYYY-MM-DD when the text holds a complete date (day, month and year),
    else None. Fragments such as "31", "28", "07/2025" or "juillet 2025" return None.

    Accepts ISO dates, numeric dates with / . - separators ("31/07/2025", "31.07.25"),
    and month names in French, Spanish or English ("31 juillet 2025", "21-Aug-2025",
    "December 31, 2024", "le 18 Septembre 2025"). order ("DMY" or "MDY") settles numeric
    dates where both the first two numbers are <= 12.
    """
    if value is None:
        return None
    s = _fold(_clean(str(value)))
    if not s:
        return None

    m = re.search(r"\b(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})\b", s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = re.search(r"\b(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4}|\d{2})\b", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        elif (order or "DMY").upper() == "MDY":
            month, day = a, b
        else:
            day, month = a, b
        return _iso(y, month if 1 <= month <= 12 else None, day)

    # Month written as a word: day before ("31 juillet 2025", "1er août 2025", "21-Aug-25")
    # or after ("December 31, 2024").
    m = re.search(r"\b(\d{1,2})(?:er|st|nd|rd|th)?[\s.\-/]*(?:de\s+)?([a-z]{3,})\.?[\s.,\-/]*(?:de\s+|del\s+)?(\d{4}|\d{2})\b", s)
    if m:
        return _iso(_year(m.group(3)), _month_from_word(m.group(2)), int(m.group(1)))
    m = re.search(r"\b([a-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", s)
    if m:
        return _iso(_year(m.group(3)), _month_from_word(m.group(1)), int(m.group(2)))
    return None
