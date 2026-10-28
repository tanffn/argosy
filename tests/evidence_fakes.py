"""Explicit isolation seam for older flow tests; real review has its own tests."""
async def accept_author(*, author, persist, **kwargs):
    report = await author("Test evidence review")
    await persist(report)
    return report, {"status": "approved", "audits": []}
