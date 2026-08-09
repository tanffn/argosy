"""The validated event spine — PHASE 1 (the integrity floor).

See ``docs/design/argosy_operating_model_spec.md`` §2A/§3. This slice ships the
integrity producer (``integrity.py``: content hash + conservation assessment +
verdict recording) and the gate accessor (``validated_snapshot.py``). The period
/ attribution finalizers, the contribution ledger, and the ~19-reader cut-over
are deliberately deferred to later slices.
"""
