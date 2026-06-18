def test_public_exports():
    import argosy.state.graph_store as gs
    for name in ("save_graph", "load_graph", "apply_change",
                 "replay_cycle", "ReplayStep", "RecipeRegistry"):
        assert name in gs.__all__
