"""Per-issuer portfolio-snapshot parsers.

Add a new parser here when a new issuer (Schwab CSV, Interactive
Brokers CSV, etc.) needs its own portfolio-positions ingest. Each
parser exposes:
  - PARSER_NAME / PARSER_VERSION constants
  - is_<issuer>_xls(content) sniffer (cheap content check)
  - parse_<issuer>_<format>(content) -> structured snapshot

The dispatcher in `argosy.services.portfolio_ingest.dispatch` (when
it lands -- single parser today doesn't need one) sniffs the upload
bytes and routes to the right parser.
"""
