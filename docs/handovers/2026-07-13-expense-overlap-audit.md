# Expense overlap audit — 2026-07-13

## How to reproduce (confirmer)

Working dir: `D:\Projects\financial-advisor`. DB: `db/argosy.db` (read-only).

```powershell
# From repo root — read-only inspect:
$env:PYTHONIOENCODING='utf-8'
D:\Projects\financial-advisor\.venv\Scripts\python.exe -c "import sqlite3; print(sqlite3.connect(r'db/argosy.db').execute('select count(*) from expense_transactions').fetchone())"

# Or: sqlite3 db/argosy.db  then paste the SQL inventory below.
```

### Key definition (bank)

Duplicate group = same `source_id`, **different** `statement_id`, identical:

`(occurred_on, merchant_raw, direction, reference, amount_nis, amount_orig, currency_orig)`

with `COUNT(*) > 1` across statements.

### Cards

Same key. Rows with `tx_type='installment'` are listed separately (not counted as "bug extras" — installment schedules legitimately repeat amount/merchant keys across cycles). Non-installment cross-statement dups are bug extras.

### SQL inventory (all sources)

```sql
WITH keyed AS (
  SELECT
    t.id AS tx_id,
    t.source_id,
    t.statement_id,
    t.tx_type,
    t.occurred_on,
    t.merchant_raw,
    t.direction,
    t.reference,
    t.amount_nis,
    t.amount_orig,
    t.currency_orig
  FROM expense_transactions t
),
groups AS (
  SELECT
    source_id,
    occurred_on, merchant_raw, direction, reference,
    amount_nis, amount_orig, currency_orig,
    COUNT(*) AS n,
    COUNT(DISTINCT statement_id) AS n_stmts,
    GROUP_CONCAT(DISTINCT statement_id) AS statement_ids,
    GROUP_CONCAT(tx_id) AS tx_ids,
    MIN(statement_id) AS keep_statement_id
  FROM keyed
  GROUP BY source_id, occurred_on, merchant_raw, direction, reference,
           amount_nis, amount_orig, currency_orig
  HAVING COUNT(DISTINCT statement_id) > 1
)
SELECT
  s.display_name,
  g.*,
  CASE WHEN EXISTS (
    SELECT 1 FROM keyed k
    WHERE k.source_id = g.source_id
      AND k.occurred_on IS g.occurred_on
      AND IFNULL(k.merchant_raw,'') = IFNULL(g.merchant_raw,'')
      AND k.direction = g.direction
      AND IFNULL(k.reference,'') = IFNULL(g.reference,'')
      AND k.amount_nis = g.amount_nis
      AND IFNULL(k.amount_orig,0) = IFNULL(g.amount_orig,0)
      AND IFNULL(k.currency_orig,'') = IFNULL(g.currency_orig,'')
      AND k.tx_type = 'installment'
  ) THEN 1 ELSE 0 END AS has_installment
FROM groups g
JOIN expense_sources s ON s.id = g.source_id
ORDER BY s.display_name, g.occurred_on, g.merchant_raw;

```

Join file names:

```sql
SELECT es.id AS statement_id, es.source_id, es.period_start, es.period_end,
       es.file_id, uf.original_name
FROM expense_statements es
LEFT JOIN user_files uf ON uf.id = es.file_id
ORDER BY es.source_id, es.id;

```

Generated from live DB at 2026-07-13T22:46:14.

## Live proof markers

- Stmt **57**: source_id=5, period=2026-03-10→2026-06-10, ingested_at=2026-06-12 12:27:54.924704 — ingested **before** bank overlap fix (`de26712` 2026-07-12 19:41 +0300 / `b770d32` 2026-07-12 19:33 +0300).
- Stmt **69**: source_id=5, period=2026-04-15→2026-07-10, ingested_at=2026-07-13 07:50:30.596415 — cross-statement overlap groups involving this stmt: **0** (0 = live proof bank overlap path works).

## Inventory — every duplicate group

### Leumi OSH

source_id=5, kind=bank, issuer=leumi, external_id=44745280, display_name='Leumi current account'

#### Bug extras (cross-statement dups)

| occurred_on | merchant_raw | direction | reference | amount_nis | amount_orig | currency | statement_ids | tx_ids | keep_stmt | keep_tx | delete_tx | files |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-03-10 | בנק הפועלים | credit | 99012 | 6563.79 | None |  | 54,57 | 1947,2218 | 54 | 1947 | 2218 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-10 | מקס איט פיננ-י | debit | 34685 | 7342.38 | None |  | 54,57 | 1948,2219 | 54 | 1948 | 2219 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-15 | כרטיסי אשראי-י | debit | 8547 | 4367 | None |  | 54,57 | 1944,2215 | 54 | 1944 | 2215 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-15 | ל.מאסטרקרד(יש) | debit | 1266 | 238.92 | None |  | 54,57 | 1946,2217 | 54 | 1946 | 2217 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-15 | לאומי למשכנת-י | debit | 771 | 3074.42 | None |  | 54,57 | 1945,2216 | 54 | 1945 | 2216 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-17 | קצבת ילדים-י | credit | 13104 | 276 | None |  | 54,57 | 1943,2214 | 54 | 1943 | 2214 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-24 | מקס איט פיננ-י | debit | 34685 | 137.26 | None |  | 54,57 | 1942,2213 | 54 | 1942 | 2213 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-25 | קניה-אינטרנט | debit | 97775 | 30845.58 | None |  | 54,57 | 1941,2212 | 54 | 1941 | 2212 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-30 | עיריית חיפה-י | credit | 4156 | 11658.35 | None |  | 54,57 | 1940,2211 | 54 | 1940 | 2211 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-31 | מס הכנסה | debit | 29424 | 5.01 | None |  | 54,57 | 1938,2209 | 54 | 1938 | 2209 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-03-31 | רבית זכות | credit | 29423 | 33.42 | None |  | 54,57 | 1937,2208 | 54 | 1937 | 2208 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-01 | מלאנוקס טכנו-י | credit | 61307 | 24249.3 | None |  | 54,57 | 1939,2210 | 54 | 1939 | 2210 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-03 | מקס איט פיננ-י | debit | 34685 | 0.02 | None |  | 54,57 | 1936,2207 | 54 | 1936 | 2207 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-10 | ל.מאסטרקרד(יש) | debit | 235 | 1970 | None |  | 54,57 | 1935,2206 | 54 | 1935 | 2206 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-10 | מקס איט פיננ-י | debit | 34685 | 9748.85 | None |  | 54,57 | 1934,2205 | 54 | 1934 | 2205 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-15 | כרטיסי אשראי-י | debit | 8547 | 654.88 | None |  | 54,57 | 1931,2202 | 54 | 1931 | 2202 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-15 | ל.מאסטרקרד(יש) | debit | 1266 | 3319.44 | None |  | 54,57 | 1933,2204 | 54 | 1933 | 2204 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-15 | לאומי למשכנת-י | debit | 771 | 3189.62 | None |  | 54,57 | 1932,2203 | 54 | 1932 | 2203 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-04-20 | קצבת ילדים-י | credit | 13104 | 276 | None |  | 54,57 | 1930,2201 | 54 | 1930 | 2201 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-05-01 | מלאנוקס טכנו-י | credit | 61307 | 25990.4 | None |  | 54,57 | 1929,2200 | 54 | 1929 | 2200 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-05-05 | מקס איט פיננ-י | debit | 34685 | 0.07 | None |  | 54,57 | 1926,2197 | 54 | 1926 | 2197 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-05-05 | עיריית חיפה-י | credit | 4156 | 11834.98 | None |  | 54,57 | 1928,2199 | 54 | 1928 | 2199 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |
| 2026-05-05 | קניה-אינטרנט | debit | 97775 | 33133.18 | None |  | 54,57 | 1927,2198 | 54 | 1927 | 2198 | 54:leumi_2026_May_Osh.xls; 57:leumi_jun12_Osh.xls |

**Extras to delete in this subsection:** 23

### Leumi USD

source_id=6, kind=bank, issuer=leumi, external_id=44745200, display_name='Leumi USD account'

#### Bug extras (cross-statement dups)

| occurred_on | merchant_raw | direction | reference | amount_nis | amount_orig | currency | statement_ids | tx_ids | keep_stmt | keep_tx | delete_tx | files |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2025-09-29 | נ"ע רבית/דו | credit | 200817 | None | 12.21 | USD | 55,56 | 2057,2153 | 55 | 2057 | 2153 | 55:usd.xls; 56:usd.xls |
| 2025-10-09 | נ"ע רבית/דו | credit | 756651 | None | 337.5 | USD | 55,56 | 2065,2140 | 55 | 2065 | 2140 | 55:usd.xls; 56:usd.xls |
| 2025-10-30 | נ"ע רבית/דו | credit | 200817 | None | 9.22 | USD | 55,56 | 2077,2154 | 55 | 2077 | 2154 | 55:usd.xls; 56:usd.xls |
| 2025-11-26 | נ"ע רבית/דו | credit | 200817 | None | 5.86 | USD | 55,56 | 2091,2142 | 55 | 2091 | 2142 | 55:usd.xls; 56:usd.xls |
| 2025-12-16 | נ"ע רבית/דו | credit | 200817 | None | 6.41 | USD | 55,56 | 2104,2136 | 55 | 2104 | 2136 | 55:usd.xls; 56:usd.xls |
| 2026-03-31 | נ"ע רבית/דו | credit | 419542 | None | 28.09 | USD | 56,58 | 2168,2238 | 56 | 2168 | 2238 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-03-31 | ניכוי מס | credit | 156386 | None | 13.39 | USD | 56,58 | 2167,2239 | 56 | 2167 | 2239 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-04-07 | המרה-אינטרנט | debit | 700016989 | None | 40000 | USD | 56,58 | 2169,2237 | 56 | 2169 | 2237 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-04-07 | נ"ע רבית/דו | credit | 156386 | None | 230.49 | USD | 56,58 | 2170,2236 | 56 | 2170 | 2236 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-04-15 | ל.מסטרקארד__ | debit | 1266 | None | 12.18 | USD | 56,58 | 2171,2235 | 56 | 2171 | 2235 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-04-15 | נ"ע רבית/דו | credit | 685397 | None | 60.86 | USD | 56,58 | 2172,2234 | 56 | 2172 | 2234 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-04-29 | העברת כספים | credit | 58581822 | None | 150864.02 | USD | 56,58 | 2173,2233 | 56 | 2173 | 2233 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-05-04 | נ"ע בבורסה | debit | 102170 | None | 13101.41 | USD | 56,58 | 2174,2232 | 56 | 2174 | 2232 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-05-04 | נ"ע בבורסה | debit | 425519 | None | 16652.34 | USD | 56,58 | 2175,2231 | 56 | 2175 | 2231 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-05-06 | נ"ע רבית/דו | credit | 156386 | None | 234.62 | USD | 56,58 | 2176,2230 | 56 | 2176 | 2230 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-05-08 | נ"ע בבורסה | debit | 187511 | None | 9070.63 | USD | 56,58 | 2177,2229 | 56 | 2177 | 2229 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-05-08 | נ"ע בבורסה | debit | 318437 | None | 3922 | USD | 56,58 | 2178,2228 | 56 | 2178 | 2228 | 56:usd.xls; 58:leumi_jun12_usd.xls |
| 2026-05-08 | נ"ע בבורסה | debit | 733344 | None | 11721.36 | USD | 56,58 | 2179,2227 | 56 | 2179 | 2227 | 56:usd.xls; 58:leumi_jun12_usd.xls |

**Extras to delete in this subsection:** 18

### Max 2923

source_id=3, kind=card, issuer=max, external_id=2923, display_name='Max 2923'

#### Bug extras (cross-statement dups; installments excluded)

| occurred_on | merchant_raw | direction | reference | amount_nis | amount_orig | currency | statement_ids | tx_ids | keep_stmt | keep_tx | delete_tx | files |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-04-10 | המכולת קריית טבעון | debit |  | 61.4 | None |  | 48,61 | 1788,2330 | 48 | 1788 | 2330 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-10 | פנדה הום בע"מ | debit |  | 530.6 | None |  | 48,61 | 1787,2329 | 48 | 1787 | 2329 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-10 | שני פסטרי | debit |  | 98 | None |  | 48,61 | 1786,2328 | 48 | 1786 | 2328 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-12 | הזמנה משלוח אוכל | debit |  | 192 | None |  | 48,61 | 1790,2332 | 48 | 1790 | 2332 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-12 | משרד התחבורה - רשיונות רכ | debit |  | 1211 | None |  | 48,61 | 1789,2331 | 48 | 1789 | 2331 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-13 | ברטה איטלקיה במשאית- קפה | debit |  | 40 | None |  | 48,61 | 1791,2333 | 48 | 1791 | 2333 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-14 | ברטה איטלקיה במשאית- קפה | debit |  | 26 | None |  | 48,61 | 1794,2336 | 48 | 1794 | 2336 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-14 | פוראבר טולד FOREVERTOLD | debit |  | 376.2 | None |  | 48,61 | 1793,2335 | 48 | 1793 | 2335 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-14 | פז אפליקציית יילו | debit |  | 18 | None |  | 48,61 | 1792,2334 | 48 | 1792 | 2334 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-15 | סאלח דאבח צ'ק פוסט חיפה | debit |  | 85.61 | None |  | 48,61 | 1795,2337 | 48 | 1795 | 2337 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-15 | רשת מתנסים קרית טבעון- או | debit |  | 40 | None |  | 48,61 | 1796,2338 | 48 | 1796 | 2338 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-16 | CARREFOUR טבעון | debit |  | 122.2 | None |  | 48,61 | 1797,2339 | 48 | 1797 | 2339 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-16 | מאי מרקט | debit |  | 61.01 | None |  | 48,61 | 1798,2340 | 48 | 1798 | 2340 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-16 | מאי מרקט | debit |  | 71.18 | None |  | 48,61 | 1799,2341 | 48 | 1799 | 2341 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-19 | בורקס העגלה רמת ישי | debit |  | 78 | None |  | 48,61 | 1802,2344 | 48 | 1802 | 2344 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-19 | ביחד בשבילך | debit |  | 50 | None |  | 48,61 | 1800,2342 | 48 | 1800 | 2342 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-19 | דור אלון עוקף קריות | debit |  | 252.08 | None |  | 48,61 | 1801,2343 | 48 | 1801 | 2343 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-20 | מאפיית השלום | debit |  | 52 | None |  | 48,61 | 1803,2345 | 48 | 1803 | 2345 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-20 | סעוד הראל -כללית | debit |  | 71.41 | None |  | 48,61 | 1804,2346 | 48 | 1804 | 2346 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-20 | שרותי בריאות כללית הו"ק | debit |  | 164.88 | None |  | 48,61 | 1805,2347 | 48 | 1805 | 2347 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-21 | סטופ מרקט בעמ | debit |  | 340.61 | None |  | 48,61 | 1806,2348 | 48 | 1806 | 2348 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-23 | יין בעיר | debit |  | 62 | None |  | 48,61 | 1809,2351 | 48 | 1809 | 2351 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-23 | מחסני השוק יוקנעם | debit |  | 56.88 | None |  | 48,61 | 1807,2349 | 48 | 1807 | 2349 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-23 | מכון רישוי מבוא כרמל | debit |  | 121.93 | None |  | 48,61 | 1808,2350 | 48 | 1808 | 2350 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-24 | א.ר נולדג' הסופר החברתי ש | debit |  | 390.7 | None |  | 48,61 | 1810,2352 | 48 | 1810 | 2352 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-24 | דומינוס רמת ישי | debit |  | 172.7 | None |  | 48,61 | 1811,2353 | 48 | 1811 | 2353 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-25 | B-FRESH CINEMALL  (סינמה | debit |  | 58 | None |  | 48,61 | 1814,2356 | 48 | 1814 | 2356 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-25 | אליטל חוצות המפרץ | debit |  | 482.92 | None |  | 48,61 | 1812,2354 | 48 | 1812 | 2354 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-25 | פאב מסעדה כאמל בע"מ | debit |  | 300 | None |  | 48,61 | 1813,2355 | 48 | 1813 | 2355 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-26 | מתנ"ס טבעון | debit |  | 440 | None |  | 48,61 | 1815,2357 | 48 | 1815 | 2357 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-27 | פנגו חשבונית חודשית | debit |  | 0.45 | None |  | 48,61 | 1816,2358 | 48 | 1816 | 2358 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-28 | המכולת קריית טבעון | debit |  | 19.8 | None |  | 48,61 | 1818,2360 | 48 | 1818 | 2360 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-28 | שופרסל שלי טבעון צפון | debit |  | 182.4 | None |  | 48,61 | 1817,2359 | 48 | 1817 | 2359 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-29 | כלל ביטוח בריאות הוק | debit |  | 247.71 | None |  | 48,61 | 1819,2361 | 48 | 1819 | 2361 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-29 | פז אפליקציית יילו | debit |  | 256.1 | None |  | 48,61 | 1820,2362 | 48 | 1820 | 2362 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-30 | גרג תיאטרון מוצקין | debit |  | 12 | None |  | 48,61 | 1821,2363 | 48 | 1821 | 2363 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |
| 2026-04-30 | מעיינות העמקים בע"מ | debit |  | 186.45 | None |  | 48,61 | 1822,2364 | 48 | 1822 | 2364 | 48:transaction-details_export_1778322493817.xlsx; 61:05_2026.xlsx |

**Extras to delete in this subsection:** 37

#### Installments only (not counted as bug extras)

| occurred_on | merchant_raw | direction | reference | amount_nis | amount_orig | currency | statement_ids | tx_ids | keep_stmt | keep_tx | delete_tx | files |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2025-12-02 | אייבורי מחשבים | debit |  | 415.5 | None |  | 25,61 | 1002,2326 | 25 | 1002 | 2326 | 25:transaction-details_export_1778322338534.xlsx; 61:05_2026.xlsx |

**Extras to delete in this subsection:** 1

### Cal 6225

source_id=4, kind=card, issuer=cal, external_id=6225, display_name='Cal 6225'

#### Bug extras (cross-statement dups; installments excluded)

_None._

#### Installments only (not counted as bug extras)

| occurred_on | merchant_raw | direction | reference | amount_nis | amount_orig | currency | statement_ids | tx_ids | keep_stmt | keep_tx | delete_tx | files |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2024-10-10 | הפניקס רכב חובה | debit |  | 205 | None |  | 29,30 | 1144,1155 | 29 | 1144 | 1155 | 29:Feb.xlsx; 30:Jan.xlsx |
| 2025-08-29 | הפניקס רכב חובה | debit |  | 259 | None |  | 28,35,36,51 | 1135,1247,1324,1891 | 28 | 1135 | 1247,1324,1891 | 28:Dec.xlsx; 35:Nov.xlsx; 36:Oct.xlsx; 51:Jan.xlsx |

**Extras to delete in this subsection:** 4

## Totals — extras to delete per statement

| statement_id | source | period | file | extras_to_delete |
|---|---|---|---|---|
| 56 | Leumi USD account | 2025-09-29→2026-05-08 | usd.xls | 5 |
| 57 | Leumi current account | 2026-03-10→2026-06-10 | leumi_jun12_Osh.xls | 23 |
| 58 | Leumi USD account | 2026-03-31→2026-06-08 | leumi_jun12_usd.xls | 13 |
| 61 | Max 2923 | 2025-12-02→2026-05-07 | 05_2026.xlsx | 37 |

**Total bug extras (tx rows to delete):** 78

### Section rollup

| section | bug_groups | bug_extras | installment_noted_extras |
|---|---:|---:|---:|
| Leumi OSH | 23 | 23 | 0 |
| Leumi USD | 18 | 18 | 0 |
| Max 2923 | 37 | 37 | 1 |
| Cal 6225 | 0 | 0 | 4 |

## How we tested the fix will prevent recurrence

- Unit tests:
  - `tests/test_expense_parsers_unit.py::test_discount_parser_marks_rolling_for_overlap_dedup`
  - `tests/test_statement_reconciliation.py::test_card_overlap_dedup_without_continuity_noise`
  - `tests/test_statement_reconciliation.py::test_check_continuity_false_skips_gap_warnings`
  - plus full files: `test_expense_parsers_unit.py`, `test_statement_reconciliation.py`, `test_expense_persistence.py`
- Git commits that fixed bank path: `b770d32` (2026-07-12 19:33 +0300, Max rolling + source-wide overlap dedup) then `de26712` (2026-07-12 19:41 +0300, bank range-export overlap dedup + installment safety). Stmt 57 was ingested before those fixes and still carries historical extras.
- Stmt 69 (Jul 13 ingest) has **0** cross-statement overlap dup groups involving it — live proof the bank reconcile path works for new uploads.
- Discount parser now sets `rolling=True`; orchestrator gate is `if src.kind == "bank" or getattr(result, "rolling", False)` with `check_continuity=(src.kind == "bank")` so rolling cards get overlap-dedup only.

## Cleanup proposal (DO NOT RUN YET)

Keep the row on the **older / min `statement_id`**; delete the extras.

```sql
-- DRY RUN first:
SELECT id, source_id, statement_id, occurred_on, merchant_raw, amount_nis FROM expense_transactions WHERE id IN (2136, 2140, 2142, 2153, 2154, 2197, 2198, 2199, 2200, 2201, 2202, 2203, 2204, 2205, 2206, 2207, 2208, 2209, 2210, 2211, 2212, 2213, 2214, 2215, 2216, 2217, 2218, 2219, 2227, 2228, 2229, 2230, 2231, 2232, 2233, 2234, 2235, 2236, 2237, 2238, 2239, 2328, 2329, 2330, 2331, 2332, 2333, 2334, 2335, 2336, 2337, 2338, 2339, 2340, 2341, 2342, 2343, 2344, 2345, 2346, 2347, 2348, 2349, 2350, 2351, 2352, 2353, 2354, 2355, 2356, 2357, 2358, 2359, 2360, 2361, 2362, 2363, 2364);

-- DELETE (only after confirmer sign-off):
DELETE FROM expense_transactions WHERE id IN (2136, 2140, 2142, 2153, 2154, 2197, 2198, 2199, 2200, 2201, 2202, 2203, 2204, 2205, 2206, 2207, 2208, 2209, 2210, 2211, 2212, 2213, 2214, 2215, 2216, 2217, 2218, 2219, 2227, 2228, 2229, 2230, 2231, 2232, 2233, 2234, 2235, 2236, 2237, 2238, 2239, 2328, 2329, 2330, 2331, 2332, 2333, 2334, 2335, 2336, 2337, 2338, 2339, 2340, 2341, 2342, 2343, 2344, 2345, 2346, 2347, 2348, 2349, 2350, 2351, 2352, 2353, 2354, 2355, 2356, 2357, 2358, 2359, 2360, 2361, 2362, 2363, 2364);
```

Python pseudocode:

```python
DELETE_IDS = [2136, 2140, 2142, 2153, 2154, 2197, 2198, 2199, 2200, 2201, 2202, 2203, 2204, 2205, 2206, 2207, 2208, 2209, 2210, 2211, 2212, 2213, 2214, 2215, 2216, 2217, 2218, 2219, 2227, 2228, 2229, 2230, 2231, 2232, 2233, 2234, 2235, 2236, 2237, 2238, 2239, 2328, 2329, 2330, 2331, 2332, 2333, 2334, 2335, 2336, 2337, 2338, 2339, 2340, 2341, 2342, 2343, 2344, 2345, 2346, 2347, 2348, 2349, 2350, 2351, 2352, 2353, 2354, 2355, 2356, 2357, 2358, 2359, 2360, 2361, 2362, 2363, 2364]
session.execute(delete(ExpenseTransaction).where(ExpenseTransaction.id.in_(DELETE_IDS)))
session.commit()  # only after explicit approval
```

**Do not run cleanup until confirmer approves.** Do not wipe the live DB beyond these targeted ids.

## Full group listing

One line per duplicate group. Keep = min statement_id; delete = other statement(s) in the group.

Format: source|occurred_on|merchant|amount|ref|keep_stmt|delete_stmt|keep_tx_id|delete_tx_id

`
Cal 6225|2024-10-10|הפניקס רכב חובה|205||29|30|1144|1155
Cal 6225|2025-08-29|הפניקס רכב חובה|259||28|35,36,51|1135|1247,1324,1891
Leumi USD account|2025-09-29|נ"ע רבית/דו||200817|55|56|2057|2153
Leumi USD account|2025-10-09|נ"ע רבית/דו||756651|55|56|2065|2140
Leumi USD account|2025-10-30|נ"ע רבית/דו||200817|55|56|2077|2154
Leumi USD account|2025-11-26|נ"ע רבית/דו||200817|55|56|2091|2142
Leumi USD account|2025-12-16|נ"ע רבית/דו||200817|55|56|2104|2136
Leumi USD account|2026-03-31|נ"ע רבית/דו||419542|56|58|2168|2238
Leumi USD account|2026-03-31|ניכוי מס||156386|56|58|2167|2239
Leumi USD account|2026-04-07|המרה-אינטרנט||700016989|56|58|2169|2237
Leumi USD account|2026-04-07|נ"ע רבית/דו||156386|56|58|2170|2236
Leumi USD account|2026-04-15|ל.מסטרקארד__||1266|56|58|2171|2235
Leumi USD account|2026-04-15|נ"ע רבית/דו||685397|56|58|2172|2234
Leumi USD account|2026-04-29|העברת כספים||58581822|56|58|2173|2233
Leumi USD account|2026-05-04|נ"ע בבורסה||102170|56|58|2174|2232
Leumi USD account|2026-05-04|נ"ע בבורסה||425519|56|58|2175|2231
Leumi USD account|2026-05-06|נ"ע רבית/דו||156386|56|58|2176|2230
Leumi USD account|2026-05-08|נ"ע בבורסה||187511|56|58|2177|2229
Leumi USD account|2026-05-08|נ"ע בבורסה||318437|56|58|2178|2228
Leumi USD account|2026-05-08|נ"ע בבורסה||733344|56|58|2179|2227
Leumi current account|2026-03-10|בנק הפועלים|6563.79|99012|54|57|1947|2218
Leumi current account|2026-03-10|מקס איט פיננ-י|7342.38|34685|54|57|1948|2219
Leumi current account|2026-03-15|כרטיסי אשראי-י|4367|8547|54|57|1944|2215
Leumi current account|2026-03-15|ל.מאסטרקרד(יש)|238.92|1266|54|57|1946|2217
Leumi current account|2026-03-15|לאומי למשכנת-י|3074.42|771|54|57|1945|2216
Leumi current account|2026-03-17|קצבת ילדים-י|276|13104|54|57|1943|2214
Leumi current account|2026-03-24|מקס איט פיננ-י|137.26|34685|54|57|1942|2213
Leumi current account|2026-03-25|קניה-אינטרנט|30845.58|97775|54|57|1941|2212
Leumi current account|2026-03-30|עיריית חיפה-י|11658.35|4156|54|57|1940|2211
Leumi current account|2026-03-31|מס הכנסה|5.01|29424|54|57|1938|2209
Leumi current account|2026-03-31|רבית זכות|33.42|29423|54|57|1937|2208
Leumi current account|2026-04-01|מלאנוקס טכנו-י|24249.3|61307|54|57|1939|2210
Leumi current account|2026-04-03|מקס איט פיננ-י|0.02|34685|54|57|1936|2207
Leumi current account|2026-04-10|ל.מאסטרקרד(יש)|1970|235|54|57|1935|2206
Leumi current account|2026-04-10|מקס איט פיננ-י|9748.85|34685|54|57|1934|2205
Leumi current account|2026-04-15|כרטיסי אשראי-י|654.88|8547|54|57|1931|2202
Leumi current account|2026-04-15|ל.מאסטרקרד(יש)|3319.44|1266|54|57|1933|2204
Leumi current account|2026-04-15|לאומי למשכנת-י|3189.62|771|54|57|1932|2203
Leumi current account|2026-04-20|קצבת ילדים-י|276|13104|54|57|1930|2201
Leumi current account|2026-05-01|מלאנוקס טכנו-י|25990.4|61307|54|57|1929|2200
Leumi current account|2026-05-05|מקס איט פיננ-י|0.07|34685|54|57|1926|2197
Leumi current account|2026-05-05|עיריית חיפה-י|11834.98|4156|54|57|1928|2199
Leumi current account|2026-05-05|קניה-אינטרנט|33133.18|97775|54|57|1927|2198
Max 2923|2025-12-02|אייבורי מחשבים|415.5||25|61|1002|2326
Max 2923|2026-04-10|המכולת קריית טבעון|61.4||48|61|1788|2330
Max 2923|2026-04-10|פנדה הום בע"מ|530.6||48|61|1787|2329
Max 2923|2026-04-10|שני פסטרי|98||48|61|1786|2328
Max 2923|2026-04-12|הזמנה משלוח אוכל|192||48|61|1790|2332
Max 2923|2026-04-12|משרד התחבורה - רשיונות רכ|1211||48|61|1789|2331
Max 2923|2026-04-13|ברטה איטלקיה במשאית- קפה|40||48|61|1791|2333
Max 2923|2026-04-14|ברטה איטלקיה במשאית- קפה|26||48|61|1794|2336
Max 2923|2026-04-14|פוראבר טולד FOREVERTOLD|376.2||48|61|1793|2335
Max 2923|2026-04-14|פז אפליקציית יילו|18||48|61|1792|2334
Max 2923|2026-04-15|סאלח דאבח צ'ק פוסט חיפה|85.61||48|61|1795|2337
Max 2923|2026-04-15|רשת מתנסים קרית טבעון- או|40||48|61|1796|2338
Max 2923|2026-04-16|CARREFOUR טבעון|122.2||48|61|1797|2339
Max 2923|2026-04-16|מאי מרקט|61.01||48|61|1798|2340
Max 2923|2026-04-16|מאי מרקט|71.18||48|61|1799|2341
Max 2923|2026-04-19|בורקס העגלה רמת ישי|78||48|61|1802|2344
Max 2923|2026-04-19|ביחד בשבילך|50||48|61|1800|2342
Max 2923|2026-04-19|דור אלון עוקף קריות|252.08||48|61|1801|2343
Max 2923|2026-04-20|מאפיית השלום|52||48|61|1803|2345
Max 2923|2026-04-20|סעוד הראל -כללית|71.41||48|61|1804|2346
Max 2923|2026-04-20|שרותי בריאות כללית הו"ק|164.88||48|61|1805|2347
Max 2923|2026-04-21|סטופ מרקט בעמ|340.61||48|61|1806|2348
Max 2923|2026-04-23|יין בעיר|62||48|61|1809|2351
Max 2923|2026-04-23|מחסני השוק יוקנעם|56.88||48|61|1807|2349
Max 2923|2026-04-23|מכון רישוי מבוא כרמל|121.93||48|61|1808|2350
Max 2923|2026-04-24|א.ר נולדג' הסופר החברתי ש|390.7||48|61|1810|2352
Max 2923|2026-04-24|דומינוס רמת ישי|172.7||48|61|1811|2353
Max 2923|2026-04-25|B-FRESH CINEMALL  (סינמה|58||48|61|1814|2356
Max 2923|2026-04-25|אליטל חוצות המפרץ|482.92||48|61|1812|2354
Max 2923|2026-04-25|פאב מסעדה כאמל בע"מ|300||48|61|1813|2355
Max 2923|2026-04-26|מתנ"ס טבעון|440||48|61|1815|2357
Max 2923|2026-04-27|פנגו חשבונית חודשית|0.45||48|61|1816|2358
Max 2923|2026-04-28|המכולת קריית טבעון|19.8||48|61|1818|2360
Max 2923|2026-04-28|שופרסל שלי טבעון צפון|182.4||48|61|1817|2359
Max 2923|2026-04-29|כלל ביטוח בריאות הוק|247.71||48|61|1819|2361
Max 2923|2026-04-29|פז אפליקציית יילו|256.1||48|61|1820|2362
Max 2923|2026-04-30|גרג תיאטרון מוצקין|12||48|61|1821|2363
Max 2923|2026-04-30|מעיינות העמקים בע"מ|186.45||48|61|1822|2364
`

Total groups listed: 81
