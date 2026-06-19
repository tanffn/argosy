"""Behavior-preservation tests for the extracted net-worth-incl-residence
helper. The contract is: this helper == the prior ``wealth_dashboard._net_worth``
body, verbatim. The dashboard's own suite (test_*dashboard*/test_real_estate*)
additionally proves the extraction changed nothing.
"""

from argosy.services.net_worth_bases import total_net_worth_incl_residence


class _Snap:
    def __init__(self, totals, positions="[]", real_estate="[]", fx=3.0):
        self.totals_json = totals
        self.positions_json = positions
        self.real_estate_json = real_estate
        self.fx_usd_nis = fx


def test_total_swaps_legacy_re_stub_for_full_net_equity():
    # value_local is LOCAL units. Home 800,000 USD - Loan 300,000 USD = 500,000 net equity.
    snap = _Snap(
        totals='{"total_usd_value_k": 4000.0}',                       # $4.00M incl. legacy stub
        positions='[{"asset_type":"real estate","currency":"USD","usd_value_k":69.0}]',
        real_estate='[{"location":"Home","currency":"USD","role":"Home","value_local":800000.0},'
                    ' {"location":"Home","currency":"USD","role":"Loan","value_local":300000.0}]',
    )
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=snap, fx_usd_nis=3.0, session=None, user_id=None)
    # base 4,000,000 USD - 69,000 stub + 500,000 net equity = 4,431,000 USD; ×3.0 = NIS.
    assert nw_usd == 4_431_000.0
    assert nw_nis == 4_431_000.0 * 3.0


def test_missing_total_returns_none():
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=_Snap(totals="{}"), fx_usd_nis=3.0, session=None, user_id=None)
    assert nw_nis is None and nw_usd is None


def test_malformed_real_estate_json_falls_back_to_base():
    snap = _Snap(totals='{"total_usd_value_k": 1000.0}', real_estate="not json")
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=snap, fx_usd_nis=3.0, session=None, user_id=None)
    assert nw_usd == 1_000_000.0  # no RE applied; base preserved


def test_none_snapshot_returns_none():
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=None, fx_usd_nis=3.0, session=None, user_id=None)
    assert nw_nis is None and nw_usd is None
