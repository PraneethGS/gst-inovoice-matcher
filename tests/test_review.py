import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.review import review_report


def test_review_is_disabled_by_default():
    report = {"results": [{"status": "FUZZY_MATCH", "confidence": 0.85}]}
    asyncio.run(review_report(report, enabled=False))
    assert report["results"][0]["llm_recommendation"] is None


def test_review_only_targets_ambiguous_results(monkeypatch):
    async def fake_review(result):
        return {"decision": "ACCEPT", "reason": "Amounts and supplier agree."}

    monkeypatch.setattr("app.review.get_llm_recommendation", fake_review)
    report = {"results": [
        {"status": "FUZZY_MATCH", "confidence": 0.85},
        {"status": "FUZZY_MATCH", "confidence": 0.95},
    ]}
    asyncio.run(review_report(report, enabled=True))
    assert report["results"][0]["llm_recommendation"]["decision"] == "ACCEPT"
    assert report["results"][1]["llm_recommendation"] is None
