# argosy/services/merchant_service.py
"""Merchant↔category mapping primitive.

Single source of truth for "the user has decided what category this merchant
belongs to". Writes/updates a merchant_category_cache row AND every
expense_transactions row for this user with that merchant_normalized.

Used by:
  - PATCH /api/expenses/transactions/{id} (when apply_to_siblings=True)
  - PATCH /api/expenses/merchants/{merchant_normalized}
  - POST /api/expenses/merchants/bulk-category
"""
from __future__ import annotations
