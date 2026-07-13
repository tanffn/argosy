"""Per-format statement parsers for the household-expenses ingest path.

Format names are FORMAT families, not brands (see ``sniff.py``):
  - ``discount`` format is observed on the Max-branded card 2923
  - ``max`` format monthly sheet is used by Cal-branded card 6225;
    Cal rolling exports also route here (sheet ``פירוט עסקאות וזיכויים``)

## Card / account identity (last-4 / external_id)

| Parser        | Identity source (priority order)                                      | Needs caller hint? |
|---------------|-----------------------------------------------------------------------|--------------------|
| ``isracard``  | Title cell row 4: ``'<card type> - <last4>'``                         | No                 |
| ``discount``  | Per-row col 3 (``4 ספרות אחרונות…``), first non-empty data row        | No                 |
| ``max`` monthly | Caller ``last4_hint`` (folder/card); else sheet-name bank-acct last-4 (WRONG — Max files bill a bank account, not the card #) | **Yes** (monthly) |
| ``max`` rolling | Title ``…לכרטיס <brand> <last4>``; then hint; then ``<last4>_`` filename | No (self-id)     |
| ``leumi_osh`` | HTML account header → bare 8-digit account #                          | No                 |
| ``leumi_usd`` | HTML account header → bare 8-digit account # (פמ"ח)                   | No                 |

Orchestrator / upload route: pass ``card_last4`` only for Max-format
*monthly* files. Rolling Cal exports and every other format self-identify.
"""
