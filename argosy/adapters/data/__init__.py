"""Argosy market-data adapters (Phase 2).

All adapters share a common cache wrapper (`cache.cached_call`) and a
common keychain-first secrets pattern. Tests mock the underlying client
(yfinance / FRED / finnhub); we never call live APIs in tests.
"""
