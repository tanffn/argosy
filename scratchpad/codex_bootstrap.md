DO NOT.

1. The dependency-graph idea is valid, but `role='current'` is not a private synthesis anchor. Add `anchor_plan_version_id=119` to [`run_synthesis`](/D:/Projects/financial-advisor/argosy/orchestrator/flows/plan_synthesis/orchestrator.py:179), validate ownership/role, and use it instead of [`get_current_plan`](/D:/Projects/financial-advisor/argosy/orchestrator/flows/plan_synthesis/orchestrator.py:228). Then run the full pipeline while 92 remains current.

2. Yes. Promotion durably stamps acceptance, supersedes 92, emits current-change events, refreshes caches/narrative, and may close corrective proposals or flags. Superseding 119 later does not undo downstream actions. Also, daily brief currently reads the newest plan regardless of role, so it may already consume 119; pause it during synthesis.

3. Smallest fix: the explicit anchor parameter plus provenance recording (`anchor_plan_version_id`, role, caller, timestamp) on the decision run. Do **not** accept draft-lineage verdicts: verdicts must bind the exact reviewed artifact. If an emergency override is nevertheless owner-authorized, record machine-readable `gate_cleared=false`, every missing/blocking authority, `bootstrap_only`, incident/reason, approver, expiry, and intended successor—and isolate deploy/brief/snapshot consumers.

4. The content assessment of 119 is unchanged. My literal **PROMOTE** verdict is withdrawn: 119 may be a sound synthesis input, but it is not legitimately promotable without its required authorities.