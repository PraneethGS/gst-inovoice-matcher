"""Optional LLM review for ambiguous deterministic match results."""
import os


REVIEW_STATUSES = {"ACCEPT", "REJECT", "NEEDS_HUMAN"}


def _is_ambiguous(result: dict) -> bool:
    if result.get("status") == "FUZZY_MATCH":
        return float(result.get("confidence", 0)) < 0.90
    if result.get("status") != "AMOUNT_MISMATCH":
        return False
    left = abs(float(result.get("taxable_value_2b") or 0))
    right = abs(float(result.get("taxable_value_ledger") or 0))
    if max(left, right) == 0:
        return False
    drift = abs(left - right) / max(left, right)
    return 0.01 <= drift <= 0.03


async def get_llm_recommendation(result: dict) -> dict:
    """Ask Gemini for a bounded recommendation for one exception."""
    import httpx

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {"decision": "NEEDS_HUMAN", "reason": "LLM review is not configured."}
    prompt = (
        "Review this deterministic GST invoice reconciliation exception. "
        "Return JSON only with decision ACCEPT, REJECT, or NEEDS_HUMAN and a "
        "one-sentence reason. Do not change the deterministic status.\n"
        f"Exception: {result}"
    )
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{url}?key={api_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
    response.raise_for_status()
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    # Keep parsing deliberately conservative; malformed model output goes to a human.
    import json
    try:
        parsed = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    except (ValueError, TypeError):
        return {"decision": "NEEDS_HUMAN", "reason": "LLM returned an invalid recommendation."}
    decision = str(parsed.get("decision", "NEEDS_HUMAN")).upper()
    if decision not in REVIEW_STATUSES:
        decision = "NEEDS_HUMAN"
    return {"decision": decision, "reason": str(parsed.get("reason", "No reason provided."))[:500]}


async def review_report(report: dict, enabled: bool | None = None) -> dict:
    """Attach recommendations to ambiguous results when review is enabled."""
    if enabled is None:
        enabled = os.getenv("ENABLE_LLM_REVIEW", "false").lower() in {"1", "true", "yes"}
    for result in report.get("results", []):
        result["llm_recommendation"] = None
        if enabled and _is_ambiguous(result):
            try:
                result["llm_recommendation"] = await get_llm_recommendation(result)
            except Exception:
                result["llm_recommendation"] = {
                    "decision": "NEEDS_HUMAN", "reason": "LLM review failed; deterministic result retained."
                }
    return report
