"""Cash-view identity and posted/provisional separation in Leumi exports."""

import pytest

from argosy.services.expense_ingest.parsers.leumi_html import (
    LeumiAccountView,
    LeumiAmbiguousHeaderError,
    classify_leumi_account_view,
)
from argosy.services.expense_ingest.parsers.leumi_usd import parse
from argosy.services.expense_ingest.sniff import detect_format
from argosy.services.expense_ingest.types import ParserName


def _export(descriptor='', summary='יתרת עו"ש: 29,668.71', posted_rows=1):
    posted = ''.join(
        f'<tr><td>22/10/26</td><td>נ"ע בבורסה</td><td></td>'
        f'<td>{100+i}</td><td>10.00</td><td></td><td>50.00</td></tr>'
        for i in range(posted_rows)
    )
    # The provisional table deliberately has more rows than posted history.
    provisional = ''.join(
        f'<tr><td>30/10/26</td><td>נ"ע בבורסה</td><td>{200+i}</td>'
        '<td>20.00</td><td></td><td>30.00</td></tr>'
        for i in range(4)
    )
    return (
        '<HTML><body><span>תנועות בחשבון מט"ח</span>'
        '<script>' + 'var padding="ignored";' * 600 + '</script>'
        '<span>חשבון:</span><span>447452/00 094 דולר ארה"ב</span>'
        f'<span>{descriptor}</span><span>{summary}</span>'
        '<h2>תנועות בעו"ש</h2><table><tr><th>תאריך</th><th>תיאור</th>'
        '<th>תאור מורחב</th><th>אסמכתא</th><th>חובה</th><th>זכות</th>'
        f'<th>יתרה</th></tr>{posted}</table>'
        '<h2>תנועות היום</h2><table><tr><th>תאריך</th><th>תיאור</th>'
        '<th>אסמכתא</th><th>חובה</th><th>זכות</th><th>יתרה</th></tr>'
        f'{provisional}</table></body></HTML>'
    )


def test_modern_cash_summary_identifies_usd_without_legacy_descriptor(tmp_path):
    path = tmp_path / 'cash.xls'
    path.write_text(_export(), encoding='utf-8')
    assert detect_format(path) == ParserName.LEUMI_USD
    result = parse(path)
    assert result.source_hint.external_id == '44745200'
    assert len(result.transactions) == 1
    assert result.statement.period_end.isoformat() == '2026-10-22'
    assert result.transactions[0].reference == '100'
    assert result.transactions[0].amount_orig == 10


def test_custody_descriptor_after_large_script_still_rejected():
    assert classify_leumi_account_view(
        _export(descriptor='ני"ע נסחרים בחו"ל')
    ) == LeumiAccountView.CUSTODY


@pytest.mark.parametrize('summary', ['', 'יתרת עו"ש: unknown', 'יתרה: 29,668.71'])
def test_generic_fx_or_unconfirmed_balance_remains_ambiguous(summary):
    with pytest.raises(LeumiAmbiguousHeaderError):
        classify_leumi_account_view(_export(summary=summary))


def test_transaction_description_cannot_identify_cash_view():
    html = _export(summary='').replace('נ"ע בבורסה', 'פמ"ח יתרת עו"ש: 123.45')
    with pytest.raises(LeumiAmbiguousHeaderError):
        classify_leumi_account_view(html)


def test_provisional_only_table_is_not_posted_history(tmp_path):
    path = tmp_path / 'cash.xls'
    html = _export()
    start = html.index('<h2>תנועות בעו"ש</h2>')
    end = html.index('<h2>תנועות היום</h2>')
    path.write_text(html[:start] + html[end:], encoding='utf-8')
    with pytest.raises(ValueError, match='complete posted-ledger columns'):
        parse(path)
