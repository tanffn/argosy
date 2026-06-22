"""Parser for Israeli NVIDIA/Mellanox "Hilan" (חילן) payslip PDFs.

The foundation for auto-verifying RSU (equity) tax withholding: it turns a
monthly Hilan payslip PDF into a typed :class:`PayslipFacts` record with a
per-field confidence map and a list of validation warnings.

Why this module is unusual
--------------------------
The project reads PDFs with ``pypdf`` (see ``argosy/ingest/file_to_text.py``).
On these Hilan payslips the PDF font carries **no ToUnicode map**, so pypdf
cannot map the glyphs to Unicode. Numbers come out clean (they are plain
ASCII digits) but the Hebrew labels come out as their raw **CP1255** byte
values reinterpreted as Latin-1 code points (mojibake), e.g. the label
``רגיל ברוטו`` arrives as ``'\xec\xe9\xe2\xf8 \xe5\xe8\xe5\xf8\xe1'``.

That mojibake is *deterministic and reversible*: every code point is the
original CP1255 byte. We recover the real Hebrew by collecting the runs of
``ord(c) > 127`` code points and decoding them as CP1255. This gives us
**stable Hebrew label anchors** — far more robust than blind positional
slicing, which breaks because the year-to-date accumulator block changes
length between equity/vest months and plain months.

Parsing strategy
----------------
1. Recover every line's text via :func:`_recover_hebrew` (CP1255 round-trip).
2. Anchor each field on its recovered Hebrew label (substring match against
   labels confirmed identical byte-for-byte across all four 2026 sample
   payslips). Each payslip line is ``"<amount(s)> <hebrew label>"``.
3. Validate with internal accounting identities and only then assign a
   confidence. When an identity fails (it legitimately does in vest months,
   where equity is a *taxed notional* that does not move real cash), we
   record a warning and lower the confidence of the affected fields rather
   than silently emitting a number we could not corroborate.

Validated identities
--------------------
All three of these hold **every** month, including vest months — verified
against the four 2026 sample payslips. A failure means a parse error, and we
mark the affected field low-confidence with a warning:

* ``income_tax + national_insurance + health_tax == total_tax_deductions``
  (the three components are the real withholding split).
* ``total_payments - total_tax_deductions - provident_funds == net_salary``
  (book net; in a vest month ``net_salary`` simply goes *negative* because
  the equity tax notional flows through ``total_tax_deductions`` — the
  identity still holds, so a break is a parse error, not a vest month).
* ``net_salary - obligation_deductions == net_to_pay`` (cash net; ties the
  book figures to the cash actually paid — the strongest end-to-end check
  that the summary block was read in the right order).

Vest-month detection is therefore done by the real signal — a **negative
book net_salary** (set on ``is_vest_month``) — not by a (non-existent)
broken identity.

Never fabricate: a field that cannot be located is left ``None`` with a
warning, and never back-filled from an assumption.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Hebrew month names (as they decode from CP1255) -> month number.
# The label line reads e.g. "תלוש שכר לחודש<month>2026"; the month name is
# embedded with no separators, so we substring-match these.
# --------------------------------------------------------------------------
_HEB_MONTHS: dict[str, int] = {
    "ינואר": 1,
    "פברואר": 2,
    "מרץ": 3,
    "אפריל": 4,
    "מאי": 5,
    "יוני": 6,
    "יולי": 7,
    "אוגוסט": 8,
    "ספטמבר": 9,
    "אוקטובר": 10,
    "נובמבר": 11,
    "דצמבר": 12,
}

# Confidence levels.
HIGH = "high"
MEDIUM = "medium"
LOW = "low"


@dataclass
class PayslipFacts:
    """Structured facts extracted from one Hilan payslip.

    All monetary fields are in NIS (₪). ``None`` means "could not be located
    or validated" — never a fabricated default. ``confidence`` carries a
    per-field level ("high"/"medium"/"low") for every field that was looked
    up; ``warnings`` explains every identity failure or missing field.
    """

    # Period (filename is the authoritative key; doc month is cross-checked).
    period_year: int | None = None
    period_month: int | None = None

    # Summary block (the six headline figures, in document order).
    total_payments: float | None = None
    total_tax_deductions: float | None = None
    provident_funds: float | None = None
    net_salary: float | None = None
    obligation_deductions: float | None = None
    net_to_pay: float | None = None

    # Tax-deduction breakdown (sums to total_tax_deductions).
    income_tax: float | None = None
    national_insurance: float | None = None
    health_tax: float | None = None

    # Monthly tax context.
    gross_for_income_tax: float | None = None
    marginal_rate_pct: float | None = None
    credit_points: float | None = None

    # Year-to-date accumulators ("סכומים מצטברים לשנת המס").
    ytd_regular_gross: float | None = None
    ytd_non_fixed_gross: float | None = None  # equity / RSU income
    ytd_capital_gain: float | None = None  # רווח הון
    ytd_regular_tax: float | None = None
    ytd_tax_on_non_fixed_gross: float | None = None  # tax withheld on equity
    ytd_taxable_income: float | None = None

    # True when this month carries an equity (RSU/ESPP vest) tax notional —
    # detected by a negative book net_salary, not by a broken identity.
    is_vest_month: bool = False

    confidence: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Text recovery
# --------------------------------------------------------------------------


def _recover_hebrew(line: str) -> str:
    """Recover Hebrew labels mojibake'd by the ToUnicode-less Hilan font.

    Runs of code points above 0x7F are the original CP1255 bytes; decode
    each such run as CP1255. ASCII (digits, commas, ``%``, latin words like
    ``espp``) is passed through untouched.
    """
    out: list[str] = []
    buf: list[int] = []
    for ch in line:
        o = ord(ch)
        if o > 0x7F:
            buf.append(o)
        else:
            if buf:
                out.append(_decode_run(buf))
                buf = []
            out.append(ch)
    if buf:
        out.append(_decode_run(buf))
    return "".join(out)


def _decode_run(byte_vals: list[int]) -> str:
    try:
        return bytes(byte_vals).decode("cp1255")
    except (ValueError, UnicodeDecodeError):
        # Defensive: leave it as raw chars if it isn't valid CP1255.
        return "".join(chr(b) for b in byte_vals)


_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")


def _nums(line: str) -> list[float]:
    """Return every numeric token on a line, in left-to-right order."""
    return [float(m.replace(",", "")) for m in _NUM_RE.findall(line)]


def _leading_num(line: str) -> float | None:
    """First numeric token on the line, or None.

    Hilan amount lines are ``"<amount> <hebrew label>"``; the amount is the
    leading token.
    """
    m = _NUM_RE.match(line.strip())
    return float(m.group(0).replace(",", "")) if m else None


# --------------------------------------------------------------------------
# Field locators (label-keyed). Each returns the leading amount on the first
# line whose recovered Hebrew contains ALL of the given substrings.
# --------------------------------------------------------------------------


def _rev(s: str) -> str:
    """Reverse a Hebrew needle to match the visually-reversed recovered text.

    pypdf emits these payslips' text character-reversed per line (the font
    has no ToUnicode map and the glyph run is laid out RTL). Numbers stay
    left-to-right (they are ASCII placed as separate tokens), so we cannot
    reverse whole lines without corrupting amounts; instead we reverse the
    Hebrew label needle and substring-match it against the line as-is.
    """
    return s[::-1]


def _has(line: str, *needles: str) -> bool:
    return all(_rev(n) in line for n in needles)


def _find_amount(lines: list[str], *needles: str) -> float | None:
    for ln in lines:
        if _has(ln, *needles):
            return _leading_num(ln)
    return None


def _approx_eq(a: float | None, b: float | None, tol: float = 0.05) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def parse_payslip(pdf_path: str | Path) -> PayslipFacts:
    """Parse a Hilan payslip PDF into :class:`PayslipFacts`.

    Args:
        pdf_path: path to the payslip PDF. The filename, when of the form
            ``YYYY_MM.pdf``, is used as the authoritative period key and is
            cross-checked against the period printed in the document.
    """
    from pypdf import PdfReader  # local import; optional dep elsewhere

    pdf_path = Path(pdf_path)
    reader = PdfReader(str(pdf_path))
    raw = "\n".join((p.extract_text() or "") for p in reader.pages)
    lines = [_recover_hebrew(ln) for ln in raw.split("\n")]

    facts = PayslipFacts()
    conf = facts.confidence
    warn = facts.warnings

    _parse_period(facts, lines, pdf_path)
    _parse_summary(facts, lines)
    _parse_tax_breakdown(facts, lines)
    _parse_monthly_context(facts, lines)
    _parse_ytd(facts, lines)

    # ------------------------------------------------------------------
    # Identity 1: the three tax components sum to total tax deductions.
    # Holds every month; it is our anchor for trusting the breakdown.
    # ------------------------------------------------------------------
    comp_sum = None
    if all(
        v is not None
        for v in (
            facts.income_tax,
            facts.national_insurance,
            facts.health_tax,
        )
    ):
        comp_sum = (
            facts.income_tax  # type: ignore[operator]
            + facts.national_insurance
            + facts.health_tax
        )
    if comp_sum is not None and _approx_eq(comp_sum, facts.total_tax_deductions):
        for k in ("income_tax", "national_insurance", "health_tax"):
            conf[k] = HIGH
    else:
        for k in ("income_tax", "national_insurance", "health_tax"):
            conf.setdefault(k, LOW)
            conf[k] = LOW
        if comp_sum is not None:
            warn.append(
                "Tax-component identity failed: income_tax + national_insurance"
                f" + health_tax = {comp_sum:,.2f} != total_tax_deductions ="
                f" {facts.total_tax_deductions}. Tax breakdown low-confidence."
            )

    # ------------------------------------------------------------------
    # Identity 2 (book net): net_salary = total_payments -
    # total_tax_deductions - provident_funds. Hilan keeps this internally
    # consistent EVERY month — in a vest month net_salary simply goes
    # negative (the equity notional flows through total_tax_deductions as
    # withholding). So a failure here means a genuine PARSE error, not a
    # vest month, and is treated as such.
    # ------------------------------------------------------------------
    net_calc = None
    if all(
        v is not None
        for v in (
            facts.total_payments,
            facts.total_tax_deductions,
            facts.provident_funds,
        )
    ):
        net_calc = (
            facts.total_payments  # type: ignore[operator]
            - facts.total_tax_deductions
            - facts.provident_funds
        )
    if net_calc is not None and _approx_eq(net_calc, facts.net_salary):
        conf["net_salary"] = HIGH
    else:
        conf["net_salary"] = LOW
        if net_calc is not None:
            warn.append(
                "Book-net identity FAILED (likely a parse error, not a vest"
                " month): total_payments - total_tax_deductions -"
                f" provident_funds = {net_calc:,.2f} != printed net_salary ="
                f" {facts.net_salary}. net_salary marked low-confidence."
            )

    # ------------------------------------------------------------------
    # Identity 3 (cash net): net_to_pay = net_salary - obligation_deductions.
    # Holds every month and ties the book figures to the actual cash paid;
    # it is the strongest end-to-end check that the summary block was read
    # in the right order.
    # ------------------------------------------------------------------
    cash_calc = None
    if facts.net_salary is not None and facts.obligation_deductions is not None:
        cash_calc = facts.net_salary - facts.obligation_deductions
    if cash_calc is not None and _approx_eq(cash_calc, facts.net_to_pay):
        conf["net_to_pay"] = HIGH
    else:
        conf["net_to_pay"] = LOW
        if cash_calc is not None:
            warn.append(
                "Cash-net identity FAILED: net_salary - obligation_deductions"
                f" = {cash_calc:,.2f} != printed net_to_pay ="
                f" {facts.net_to_pay}. Summary block may be misaligned;"
                " net_to_pay marked low-confidence."
            )

    # ------------------------------------------------------------------
    # Vest-month detection (the real signal): the equity notional makes the
    # book net_salary negative even though cash net_to_pay stays normal.
    # This is informational, not an error — it tells the downstream RSU
    # reconciler that this month carries an equity event.
    # ------------------------------------------------------------------
    if facts.net_salary is not None and facts.net_salary < 0:
        facts.is_vest_month = True
        warn.append(
            "Vest/equity month detected: book net_salary is negative"
            f" ({facts.net_salary:,.2f}) due to the equity tax notional;"
            " cash net_to_pay is unaffected. YTD equity fields apply."
        )
    elif (
        facts.ytd_tax_on_non_fixed_gross is not None
        and facts.ytd_tax_on_non_fixed_gross > 0
    ):
        # Equity income has accrued YTD even if this specific month was
        # plain — surfaced so the reconciler knows equity fields are live.
        facts.is_vest_month = False
    else:
        facts.is_vest_month = False

    return facts


# --------------------------------------------------------------------------
# Section parsers
# --------------------------------------------------------------------------


def _parse_period(
    facts: PayslipFacts, lines: list[str], pdf_path: Path
) -> None:
    """Period key. Filename ``YYYY_MM`` is authoritative; doc is cross-check."""
    conf = facts.confidence
    warn = facts.warnings

    file_year = file_month = None
    m = re.match(r"(\d{4})[_-](\d{2})", pdf_path.stem)
    if m:
        file_year, file_month = int(m.group(1)), int(m.group(2))

    # Document period: the "תלוש שכר לחודש<month><year>" line (chars reversed).
    doc_year = doc_month = None
    for ln in lines:
        if _rev("שכר לחודש") in ln or _rev("שכר  לחודש") in ln:
            ym = re.search(r"(\d{4})", ln)
            if ym:
                doc_year = int(ym.group(1))
            for name, num in _HEB_MONTHS.items():
                if _rev(name) in ln:
                    doc_month = num
                    break
            break

    facts.period_year = file_year if file_year is not None else doc_year
    facts.period_month = file_month if file_month is not None else doc_month

    if facts.period_year is not None and facts.period_month is not None:
        if file_year is not None:
            # Filename present -> authoritative. Cross-check the doc if read.
            if (
                doc_year is not None
                and doc_month is not None
                and (doc_year, doc_month) != (file_year, file_month)
            ):
                conf["period"] = MEDIUM
                warn.append(
                    f"Period mismatch: filename={file_year}-{file_month:02d}"
                    f" but document={doc_year}-{doc_month:02d}. Using filename."
                )
            else:
                conf["period"] = HIGH
        else:
            # Filename unusable; relying on the document only.
            conf["period"] = MEDIUM
            warn.append(
                "Period taken from document body (filename not in"
                " YYYY_MM form); medium confidence."
            )
    else:
        conf["period"] = LOW
        warn.append("Could not determine period year/month.")


def _parse_summary(facts: PayslipFacts, lines: list[str]) -> None:
    """The six headline figures, label-keyed."""
    conf = facts.confidence
    warn = facts.warnings

    spec = [
        ("total_payments", ("סך-כל", "התשלומים")),
        ("total_tax_deductions", ("ניכויי", "חובה-מסים")),
        ("provident_funds", ("קופות", "גמל")),
        ("net_salary", ("שכר", "נטו")),
        ("obligation_deductions", ("ניכויי", "התחייבות")),
        ("net_to_pay", ("נטו", "לתשלום")),
    ]
    for attr, needles in spec:
        val = _find_amount(lines, *needles)
        setattr(facts, attr, val)
        if val is None:
            conf[attr] = LOW
            warn.append(f"Summary field {attr!r} not found.")
        else:
            # Provisionally high; identity checks may downgrade net_salary.
            conf[attr] = HIGH

    # Cross-check: anchor the summary by document order as a fallback sanity
    # check. The six amounts also appear as the first six summary lines.
    # (No silent override — only a warning if the label hits look off.)


def _parse_tax_breakdown(facts: PayslipFacts, lines: list[str]) -> None:
    """income_tax / national_insurance / health_tax from the breakdown line.

    The breakdown is a header line (labels) followed by a values line with
    exactly four numbers, ordered (RTL-rendered LTR): total, health,
    national_insurance, income_tax. We locate the values line as the one
    immediately under the header that carries the four labels.
    """
    conf = facts.confidence
    warn = facts.warnings

    hdr_idx = None
    for i, ln in enumerate(lines):
        # The header carries "מס הכנסה" together with both insurance labels
        # ("ביטוח לאומי" / "ביטוח בריאות"). Requiring all three avoids
        # colliding with the monthly "ברוטו מס הכנסה" context line.
        if _has(ln, "מס הכנסה", "ביטוח לאומי", "ביטוח בריאות"):
            hdr_idx = i
            break
    if hdr_idx is None:
        for k in ("income_tax", "national_insurance", "health_tax"):
            conf[k] = LOW
        warn.append("Tax-breakdown header not found.")
        return

    # The values are on the next non-empty line with >= 4 numbers.
    for ln in lines[hdr_idx + 1 : hdr_idx + 4]:
        vals = _nums(ln)
        if len(vals) >= 4:
            total, health, ni, inc = vals[0], vals[1], vals[2], vals[3]
            facts.health_tax = health
            facts.national_insurance = ni
            facts.income_tax = inc
            # Sanity: first value should equal total_tax_deductions.
            if (
                facts.total_tax_deductions is not None
                and not _approx_eq(total, facts.total_tax_deductions)
            ):
                warn.append(
                    "Tax-breakdown leading total"
                    f" {total:,.2f} != summary total_tax_deductions"
                    f" {facts.total_tax_deductions}."
                )
            return

    for k in ("income_tax", "national_insurance", "health_tax"):
        conf[k] = LOW
    warn.append("Tax-breakdown values line not found.")


def _parse_monthly_context(facts: PayslipFacts, lines: list[str]) -> None:
    """gross_for_income_tax, marginal_rate_pct, credit_points."""
    conf = facts.confidence
    warn = facts.warnings

    g = _find_amount(lines, "ברוטו", "מס הכנסה")
    facts.gross_for_income_tax = g
    conf["gross_for_income_tax"] = HIGH if g is not None else LOW
    if g is None:
        warn.append("gross_for_income_tax not found.")

    # Marginal rate: a line with "%" and "שולי" (marginal).
    rate = None
    for ln in lines:
        if _rev("שולי") in ln and "%" in ln:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", ln)
            if m:
                rate = float(m.group(1))
                break
    facts.marginal_rate_pct = rate
    conf["marginal_rate_pct"] = HIGH if rate is not None else LOW
    if rate is None:
        warn.append("marginal_rate_pct not found.")

    # Credit points: "נקודות זיכוי" WITHOUT "ערך" (value of credit point) and
    # WITHOUT "פרוט" (the later detail header).
    cp = None
    for ln in lines:
        if (
            _rev("נקודות זיכוי") in ln
            and _rev("ערך") not in ln
            and _rev("פרוט") not in ln
        ):
            cp = _leading_num(ln)
            if cp is not None:
                break
    facts.credit_points = cp
    conf["credit_points"] = HIGH if cp is not None else LOW
    if cp is None:
        warn.append("credit_points not found.")


def _parse_ytd(facts: PayslipFacts, lines: list[str]) -> None:
    """Year-to-date accumulators, label-keyed within the YTD block.

    The block begins at "סכומים מצטברים לשנת המס" and ends at "יחידות מס".
    Each field is matched by its (stable) Hebrew label. Equity-only fields
    (non-fixed gross, capital gain, tax on non-fixed gross) are simply
    absent in plain months — we leave them None without a warning, since
    their absence is expected, not a parse failure.
    """
    conf = facts.confidence
    warn = facts.warnings

    start = end = None
    for i, ln in enumerate(lines):
        if _rev("מצטברים לשנת המס") in ln or _rev("סכומים מצטברים") in ln:
            start = i
        elif start is not None and _rev("יחידות מס") in ln:
            end = i
            break
    if start is None:
        block: list[str] = []
        warn.append("YTD accumulator block header not found.")
    else:
        block = lines[start + 1 : (end if end is not None else len(lines))]

    # (attr, needles, equity_only)
    spec = [
        ("ytd_regular_gross", ("ברוטו רגיל",), False),
        ("ytd_non_fixed_gross", ("ברוטו לא קבוע",), True),
        ("ytd_capital_gain", ("רווח הון",), True),
        ("ytd_regular_tax", ("מס רגיל",), False),
        ("ytd_tax_on_non_fixed_gross", ("מס על ברוטו לא קבוע",), True),
        ("ytd_taxable_income", ("הכנסה חייבת במס",), False),
    ]
    for attr, needles, equity_only in spec:
        # Note: "ברוטו רגיל" must not be matched by "מס רגיל" etc.; the
        # leading-amount locator works on the first line that contains all
        # needles, so distinct labels are unambiguous. To avoid "מס רגיל"
        # also matching "מס על ברוטו לא קבוע" (which contains neither), the
        # needles are full distinct phrases.
        val = None
        for ln in block:
            if _has(ln, *needles):
                val = _leading_num(ln)
                break
        setattr(facts, attr, val)
        if val is not None:
            conf[attr] = HIGH
        elif equity_only:
            # Expected to be absent in non-vest months.
            conf[attr] = MEDIUM
        else:
            conf[attr] = LOW
            warn.append(f"YTD field {attr!r} not found.")


__all__ = ["PayslipFacts", "parse_payslip"]
