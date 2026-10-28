"""Construct new frozen discovery fixtures; originals and old seals stay intact.

No inference or future return retrieval. All source numbers are pre-cutoff facts;
the market-price receipt contains only the freeze-day close, not later returns.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from evals.fleet_calibration.lab import digest, save_new

HERE = Path(__file__).resolve().parent
PACKETS = HERE / "packets"
MANIFEST = HERE / "discovery_cases_v1.json"


def build(*, case_id, symbol, alias, freeze, partition, category, k, price, shares,
          source_url, source_date, metrics, business, developments, extra_sources=()):
    mapping = []
    fundamentals = {"agent_role": "fundamentals", "report_name": f"fundamentals/{alias}",
                    "company": f"{alias} anonymized issuer", "as_of": "T0 frozen snapshot",
                    "share_count_basis": "Latest reported weighted-average diluted shares, not a verified period-end count",
                    "shares_m": shares, "confidence": "MEDIUM"}
    for name, value in {"share_price_usd": price, **metrics}.items():
        fundamentals[name] = round(value * k, 8)
        mapping.append({"masked_field": f"analyst_reports[0].{name}", "raw_value": value,
                        "masked_value": fundamentals[name], "transform": f"multiply by{k}",
                        "units": "USD per share" if name == "share_price_usd" else "USD millions",
                        "publication_date": freeze if name == "share_price_usd" else source_date,
                        "source_url": f"https://finance.yahoo.com/quote/{symbol}/history/" if name == "share_price_usd" else source_url})
    fundamentals["market_cap_usd_m"] = round(price * shares * k, 8)
    fundamentals["market_cap_basis"] = "Derived price times diluted weighted shares; approximation, not reported market capitalization"
    mapping.append({"masked_field": "analyst_reports[0].market_cap_usd_m", "raw_value": price * shares,
                    "masked_value": fundamentals["market_cap_usd_m"], "transform": f"price*weighted shares*{k}",
                    "units": "USD millions", "publication_date": freeze,
                    "source_url": source_url, "derivation": {"price": price, "weighted_diluted_shares_m": shares}})
    sources = [{"fact": "Pre-freeze issuer results, balance sheet and outlook; all amounts and derivations below are from these original units.",
                "url": source_url, "date": source_date, "source_to_masked_mapping": mapping,
                "unscaled_share_quantity_m": shares},
               {"fact": f"Freeze-day nominal close ${price}; provider daily history read without any subsequent return observations.",
                "url": f"https://finance.yahoo.com/quote/{symbol}/history/", "date": freeze,
                "market_bar_receipt": {"date": freeze, "close": price, "currency": "USD",
                                       "source_type": "retrospective daily bar, not an archived data vintage",
                                       "construction_note": "Verified no subsequent split adjustments in provider action metadata; no subsequent prices or returns used."}},
               *extra_sources]
    packet = {"case_id": case_id, "alias": alias, "category": category,
              "grading": "entry" if category == "A" else "trap",
              "real": f"{symbol} @ {freeze}", "market_symbol": symbol, "freeze_date": freeze,
              "rescale_factor": k, "synthetic": False, "positioned": False,
              "positions": f"No existing position in {alias}. Fictional diversified portfolio; research initiation only.",
              "constraints_extra": None, "expected_classes": ["buy"] if category == "A" else ["hold"],
              "contamination_terms": [symbol], "sources": sources, "resolution": None,
              "analyst_reports": [fundamentals, {"agent_role": "news", "report_name": f"news/{alias}",
                  "business_description": business, "developments": developments, "as_of": "T0",
                  "confidence": "MEDIUM"}],
              "changelog": [{"date": "2026-10-23", "fix": "NEW discovery case version with complete original-to-masked monetary mapping, pre-freeze primary sources and explicit nominal price receipt. Original fixtures/failed runs unchanged. No historical outcome enters analyst inputs."}]}
    return packet, {"case_id": case_id, "packet_sha256": digest(packet), "partition": partition,
                    "expected_screen_go": True, "expected_grade": ["BUY"] if category == "A" else ["WATCH", "PASS"],
                    "radar_candidate": {"ticker": alias, "name": "Anonymized issuer", "score": None,
                        "families": ["FROZEN_CASE_LEAD"], "price": fundamentals["share_price_usd"],
                        "market_cap": fundamentals["market_cap_usd_m"] * 1_000_000,
                        "reasons": [business, "A just-reported earnings update creates an investigate-or-pass question. Verify fundamentals, valuation, funding and catalysts before any purchase."],
                        "dollar_volume": None, "pct_change": None},
                    "radar_scope": "Constructed historical lead, not a replay of a historical market-wide radar scan"}


def main():
    if MANIFEST.exists():
        raise ValueError("Frozen discovery manifest already exists")
    specs = [
        dict(case_id="boot_2017_discovery_v2", symbol="BOOT", alias="QPX", freeze="2017-06-02", partition="development", category="A", k=2.1,
             price=5.98, shares=27.068, source_date="2017-06-01",
             source_url="https://www.sec.gov/Archives/edgar/data/1610250/000110465917036899/a17-14427_1ex99d1.htm",
             metrics={"annual_revenue_usd_m": 629.8, "annual_gaap_net_income_usd_m": 14.2,
                      "annual_adjusted_net_income_usd_m": 14.9, "quarter_revenue_usd_m": 163.0,
                      "cash_usd_m": 8.035, "net_debt_usd_m": 225.9,
                      "next_year_net_income_low_usd_m": 14.0, "next_year_net_income_high_usd_m": 15.4},
             business="A niche specialty work-apparel and footwear retailer with physical stores and ecommerce. It serves occupational and regional lifestyle customers, not general mall fashion.",
             developments=["Just-reported annual revenue grew10.7%; same-store sales grew0.3% and the latest quarter's same-store sales fell0.9%.",
                           "Management guides next-year same-store sales flat to slightly positive and plans12 new stores. Earnings are not visibly reaccelerating yet.",
                           "Inventory per store fell5%. Debt is material. A niche moat and new-store growth are the bull argument; flat organic sales and levered retail are the bear argument."]),
        dict(case_id="gpro_2016_discovery_v2", symbol="GPRO", alias="RQM", freeze="2016-06-01", partition="development", category="B", k=.85,
             price=10.32, shares=137.543, source_date="2016-05-05",
             source_url="https://www.sec.gov/Archives/edgar/data/1500435/000150043516000088/gpro2016-03x31exh991xer.htm",
             metrics={"quarter_revenue_usd_m": 183.536, "quarter_net_income_usd_m": -107.459,
                      "quarter_operating_income_usd_m": -121.435, "quarter_operating_cashflow_usd_m": -33.265,
                      "quarter_capex_usd_m": 8.219, "cash_usd_m": 248.717,
                      "marketable_securities_usd_m": 139.951, "total_liabilities_usd_m": 249.534},
             business="A branded consumer-camera manufacturer expanding toward content software and a new adjacent flying-camera product. It has substantial brand recognition but faces improving phone cameras and product saturation.",
             developments=["Latest quarter revenue fell49.5% year-over-year; gross margin was32.5%. Management says end-customer sales were stronger than shipments while channel inventory was reduced.",
                           "A new adjacent hardware product was delayed to the holiday selling season. Management reaffirmed its full-year revenue outlook.",
                           "The cash/securities balance is not a guarantee of normalized earnings; the next product cycle could recover demand or require further price reductions."]),
        dict(case_id="aehr_2020_discovery_v1", symbol="AEHR", alias="LZX", freeze="2020-07-17", partition="reserved_evaluation", category="A", k=4.3,
             price=2.09, shares=23.06, source_date="2020-07-16",
             source_url="https://www.sec.gov/Archives/edgar/data/1040470/000165495420007778/aehr_ex991.htm",
             metrics={"annual_revenue_usd_m": 22.291, "annual_net_income_usd_m": -2.802,
                      "quarter_revenue_usd_m": 3.773, "quarter_net_income_usd_m": -2.885,
                      "cash_usd_m": 5.433, "short_term_debt_usd_m": .653, "long_term_debt_usd_m": 1.026,
                      "backlog_usd_m": 2.5, "next_year_revenue_low_usd_m": 25, "next_year_revenue_high_usd_m": 28},
             business="A small semiconductor test-and-reliability equipment supplier selling systems and recurring consumables for optical-interconnect devices and wide-bandgap power semiconductors used in electrification.",
             developments=["Latest quarter sales fell versus the prior-year quarter; customers delayed production ramps. Annual revenue modestly grew and the company remained loss-making.",
                           "Management expects next-year revenue growth12-26% and profitability. Several existing customers and more than a dozen prospects are considering production ramps over the next one-to-two fiscal years.",
                           "Backlog fell to one third of the prior-year level. Customer promises are not orders; working capital, losses and customer concentration may threaten the ramp."]),
        dict(case_id="sfix_2021_discovery_v1", symbol="SFIX", alias="TQR", freeze="2021-06-08", partition="reserved_evaluation", category="B", k=2.3,
             price=66.10, shares=106.69622, source_date="2021-06-07",
             source_url="https://www.sec.gov/Archives/edgar/data/1576942/000157694221000079/exhibit991-q321.htm",
             metrics={"quarter_revenue_usd_m": 535.589, "quarter_net_income_usd_m": -18.846,
                      "quarter_adjusted_ebitda_usd_m": 11.6, "cash_usd_m": 124.737,
                      "short_term_investments_usd_m": 98.189, "inventory_usd_m": 215.628,
                      "next_quarter_revenue_low_usd_m": 540, "next_quarter_revenue_high_usd_m": 550,
                      "current_year_revenue_low_usd_m": 2070, "current_year_revenue_high_usd_m": 2080,
                      "current_year_adjusted_ebitda_low_usd_m": 25, "current_year_adjusted_ebitda_high_usd_m": 30},
             business="An online personalized clothing and styling service combining customer data, algorithms and human stylists. It is expanding direct shopping and preview services alongside its existing curated-delivery model.",
             developments=["Latest quarter revenue grew44% and active clients grew20% to4.1million, but revenue per active client fell3% year-over-year.",
                           "Management expects next-quarter revenue growth21.8-24.0%. Current-year adjusted EBITDA margin guidance is1.2-1.4%, and the company reports a quarterly GAAP loss.",
                           "New shopping features and a leadership transition could broaden demand. Thin margins, inventory commitments and customer-acquisition economics could also limit the platform thesis."]),
    ]
    entries = []
    for cid, grade in (("nlf_synthetic_v3", ["BUY"]), ("omk_synthetic", ["WATCH", "PASS"])):
        p = json.loads((PACKETS / f"{cid}.json").read_text(encoding="utf-8"))
        f = p["analyst_reports"][0]
        entries.append({"case_id": cid, "packet_sha256": digest(p), "partition": "control",
                        "expected_screen_go": True, "expected_grade": grade,
                        "radar_candidate": {"ticker": p["alias"], "name": "Fictional control issuer", "score": None,
                            "price": f["share_price_usd"], "market_cap": f["market_cap_usd_m"] * 1e6,
                            "families": ["FROZEN_CASE_LEAD"], "reasons": [
                                "Reported business expansion and a proposed market opportunity warrant checking current earnings, valuation and customer economics. These are research leads, not purchase evidence."]},
                        "radar_scope": "Fictional screen lead; full evidence supplied only to grader"})
    for spec in specs:
        packet, entry = build(**spec)
        path = PACKETS / f"{packet['case_id']}.json"
        save_new(path, packet)
        entries.append(entry)
    save_new(MANIFEST, {"protocol": "discovery_screen_grader_v1", "created_at": datetime.now(UTC).isoformat(),
                        "cases": entries, "control_ids": ["nlf_synthetic_v3", "omk_synthetic"],
                        "limitations": ["Curated diagnostic cases, not unbiased market selection or alpha proof.",
                                        "Historical prices are retrieved later, not archived-vintage data; revisions/shape-memory remain possible.",
                                        "Reserved evaluation cases are not used for prompt tuning in this protocol; no claim of model-training novelty.",
                                        "No radar-universe scan, live analyst research, order sizing, tax, execution or portfolio simulation is replayed."]})
    print(json.dumps({"manifest": str(MANIFEST), "cases": [e["case_id"] for e in entries]}))


def repair_reviewed_facts():
    """Logged factual corrections to this new, not-yet-scored fixture set only."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    previous_manifest_hash = digest(manifest)
    fixes = {"gpro_2016_discovery_v2": {"quarter_revenue_usd_m": 183.536},
             "sfix_2021_discovery_v1": {"quarter_revenue_usd_m": 535.589, "quarter_net_income_usd_m": -18.846},
             "aehr_2020_discovery_v1": {"quarter_revenue_usd_m": 3.773}}
    names = {"BOOT": ["Boot Barn", "Sheplers", "Conroy"], "GPRO": ["GoPro", "Woodman", "HERO4", "Karma"],
             "AEHR": ["Aehr Test Systems", "Gayn Erickson", "FOX-XP", "WaferPak"],
             "SFIX": ["Stitch Fix", "Katrina Lake", "Elizabeth Spaulding"]}
    periods = {"BOOT": ("2017-04-01", "2017-04-01", "2018-03-31"),
               "GPRO": ("2016-03-31", None, None), "AEHR": ("2020-05-31", "2020-05-31", "2021-05-31"),
               "SFIX": ("2021-05-01", None, "2021-07-31")}
    for entry in manifest["cases"]:
        if entry["partition"] == "control":
            continue
        path = PACKETS / f"{entry['case_id']}.json"
        packet = json.loads(path.read_text(encoding="utf-8"))
        if digest(packet) != entry["packet_sha256"]:
            raise ValueError("Fixture changed before reviewed factual repair")
        f = packet["analyst_reports"][0]
        for key, value in fixes.get(entry["case_id"], {}).items():
            f[key] = round(value * packet["rescale_factor"], 8)
            mapping = next(m for m in packet["sources"][0]["source_to_masked_mapping"] if m["masked_field"] == f"analyst_reports[0].{key}")
            mapping.update(raw_value=value, masked_value=f[key])
        symbol = packet["market_symbol"]
        packet["contamination_terms"] = [symbol, *names[symbol]]
        if symbol == "BOOT":
            packet["analyst_reports"][1]["developments"].append(
                "The latest fiscal year and quarter had53 and14 weeks, versus52 and13 previously. The extra week supported reported growth; do not treat the full increase as comparable organic demand.")
        quarter, annual, guide = periods[symbol]
        for mapping in packet["sources"][0]["source_to_masked_mapping"]:
            field = mapping["masked_field"].split(".")[-1]
            mapping["original_units"] = "USD per share" if field == "share_price_usd" else "USD millions (financial statement tables often report USD thousands, divided by1000)"
            mapping["source_locator"] = ("Nominal market close on freeze date" if field == "share_price_usd" else
                "Approximate capitalization derived from market close and reported weighted shares" if field == "market_cap_usd_m" else
                "Issuer condensed statement/outlook row matching the named metric")
            mapping["reporting_period"] = (packet["freeze_date"] if field in ("share_price_usd", "market_cap_usd_m") else
                f"Fiscal year ended {annual}" if field.startswith("annual_") else
                f"Outlook for fiscal period ending {guide}" if field.startswith(("next_", "current_year_")) else
                f"Quarter/balance sheet ended {quarter}")
        packet["sources"][0]["share_count_period"] = f"Quarter ended {quarter} weighted-average diluted shares"
        packet["sources"][1]["market_bar_receipt"]["frozen_snapshot_boundary"] = "After close on freeze date; earliest hypothetical entry strictly later"
        packet["changelog"].append({"date": "2026-10-23", "fix":
            "Independent pre-scoring review corrected cited table values (GPRO revenue183.536; SFIX revenue535.589/net loss18.846; AEHR quarter revenue3.773), added units/period provenance and BOOT extra-week comparability, expanded issuer denylist. No expected stage labels or business theses changed."})
        path.write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
        entry["packet_sha256"] = digest(packet)
        entry["frozen_snapshot_boundary"] = "after_close"
    manifest["factual_correction"] = {"prior_manifest_sha256": previous_manifest_hash,
                                       "review": "oct23-discovery-fixture-review", "investment_expectations_changed": False}
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"factual_corrections_recorded": True, "manifest_sha256": digest(manifest)}))


def business_lead_manifest():
    """New immutable boundary version: screen sees the business, never the label."""
    previous = json.loads(MANIFEST.read_text(encoding="utf-8"))
    upgraded = json.loads(json.dumps(previous))
    upgraded["screen_contract_version"] = 2
    upgraded["screen_boundary_correction"] = {
        "prior_manifest_sha256": digest(previous),
        "diagnosis": "oct23-discovery-control-diagnosis",
        "change": "Replace identical generic stub with exact pre-existing business_description only. No financials, expected labels, hindsight or future results enter the screen.",
        "investment_expectations_changed": False,
    }
    for entry in upgraded["cases"]:
        packet = json.loads((PACKETS / f"{entry['case_id']}.json").read_text(encoding="utf-8"))
        if digest(packet) != entry["packet_sha256"]:
            raise ValueError("Packet changed")
        entry["radar_candidate"]["reasons"] = [packet["analyst_reports"][1]["business_description"]]
        entry["screen_lead_provenance"] = "analyst_reports[1].business_description"
        entry["radar_scope"] = "Business-description lead only; financial evidence and investment judgments remain downstream"
    prices = json.loads((MANIFEST.parent / "discovery_price_inputs_v1.json").read_text(encoding="utf-8"))
    prices["manifest_sha256"] = digest(upgraded)
    for path, data in [(MANIFEST.parent / "discovery_cases_v2.json", upgraded),
                       (MANIFEST.parent / "discovery_price_inputs_v2.json", prices)]:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
    print(json.dumps({"manifest_sha256": digest(upgraded), "packets_and_expectations_unchanged": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair-reviewed", action="store_true")
    parser.add_argument("--business-leads-only", action="store_true")
    args = parser.parse_args()
    if args.business_leads_only:
        business_lead_manifest()
    elif args.repair_reviewed:
        repair_reviewed_facts()
    else:
        main()
