"""Each broker adapter must conform to the BrokerAdapter Protocol."""

from __future__ import annotations

import pytest

from argosy.adapters.brokers.base import BrokerAdapter
from argosy.adapters.brokers.ibkr import IBKRAdapter
from argosy.adapters.brokers.leumi_tsv import LeumiTSVAdapter
from argosy.adapters.brokers.schwab_csv import SchwabCSVAdapter


@pytest.mark.parametrize(
    "factory",
    [
        lambda: IBKRAdapter(user_id="ariel"),
        lambda: SchwabCSVAdapter(user_id="ariel"),
        lambda: LeumiTSVAdapter(user_id="ariel"),
    ],
    ids=["ibkr", "schwab_csv", "leumi_tsv"],
)
def test_adapter_conforms_to_protocol(factory) -> None:
    adapter = factory()
    assert isinstance(adapter, BrokerAdapter)
    # Sanity: the spec attribute `name` is set.
    assert isinstance(getattr(adapter, "name", None), str)
    assert adapter.name


def test_adapter_names_are_distinct() -> None:
    names = {
        IBKRAdapter(user_id="ariel").name,
        SchwabCSVAdapter(user_id="ariel").name,
        LeumiTSVAdapter(user_id="ariel").name,
    }
    assert len(names) == 3
