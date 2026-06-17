# tests/coherence/test_migration_0069.py
import importlib.util
from pathlib import Path


def test_migration_0069_header_and_chains_from_head():
    path = Path("alembic/versions/0069_coherence_decisions.py")
    assert path.exists()
    spec = importlib.util.spec_from_file_location("m0069", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.revision == "0069_coherence_decisions"
    assert mod.down_revision == "0068_real_estate_payments"
    assert callable(mod.upgrade) and callable(mod.downgrade)
