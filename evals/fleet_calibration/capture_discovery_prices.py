"""Freeze input-price evidence only; never stores post-freeze market returns."""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evals.fleet_calibration.lab import digest, save_new

HERE = Path(__file__).resolve().parent


def main():
    import yfinance as yf
    manifest = json.loads((HERE / "discovery_cases_v1.json").read_text(encoding="utf-8"))
    rows = []
    for entry in manifest["cases"]:
        if entry["partition"] == "control":
            continue
        packet = json.loads((HERE / "packets" / f"{entry['case_id']}.json").read_text(encoding="utf-8"))
        freeze = date.fromisoformat(packet["freeze_date"])
        ticker = yf.Ticker(packet["market_symbol"])
        # No price observations after the freeze date are requested here.
        frame = ticker.history(start=str(freeze), end=str(freeze + timedelta(days=1)),
                               auto_adjust=False, raise_errors=True)
        splits = ticker.get_splits()
        later = splits[splits.index.date > freeze]
        factor = float(later.prod()) if len(later) else 1.
        close = float(frame.iloc[0]["Close"])
        rows.append({"case_id": entry["case_id"], "packet_sha256": entry["packet_sha256"],
                     "market_symbol": packet["market_symbol"], "freeze_date": str(freeze),
                     "provider_nominal_close": close, "subsequent_split_factors": {str(k.date()): float(v) for k, v in later.items()},
                     "reconstructed_original_close": close * factor,
                     "bars_requested": [str(freeze)], "source": "yfinance auto_adjust=False freeze-day bar; action-only split metadata",
                     "vintage": "retrieved retrospectively, not an archived market-data vintage",
                     "no_post_freeze_price_observations_stored": True})
    receipt = {"protocol": "discovery_frozen_input_prices_v1", "manifest_sha256": digest(manifest),
               "fetched_at": datetime.now(UTC).isoformat(), "cases": rows}
    save_new(HERE / "discovery_price_inputs_v1.json", receipt)
    print(json.dumps({"input_prices_captured": [r["case_id"] for r in rows]}))


if __name__ == "__main__":
    main()
