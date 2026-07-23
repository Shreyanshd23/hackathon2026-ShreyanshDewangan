"""
Confidence calibration
────────────────────────
The original scorecard hardcoded self-awareness points. This measures it.

Given (predicted_confidence, was_correct) pairs across a run, we compute:
  • Expected Calibration Error (ECE) — do the confidences mean anything? A
    well-calibrated agent that says "0.8" is right ~80% of the time.
  • Brier score — mean squared error of the probabilistic prediction.
  • Per-bucket reliability table — the data behind a reliability diagram.

This is the honest version of "the agent knows what it doesn't know": a number
you can track over time and regress against, not a constant.
"""

from __future__ import annotations


def calibration_report(pairs: list[tuple[float, bool]], n_buckets: int = 5) -> dict:
    """pairs: list of (confidence in [0,1], was_correct)."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "ece": None, "brier": None, "buckets": []}

    brier = sum((c - (1.0 if correct else 0.0)) ** 2 for c, correct in pairs) / n

    buckets = []
    ece = 0.0
    for b in range(n_buckets):
        lo, hi = b / n_buckets, (b + 1) / n_buckets
        # include the right edge in the last bucket
        members = [(c, ok) for c, ok in pairs if (lo <= c < hi) or (b == n_buckets - 1 and c == 1.0)]
        if not members:
            buckets.append({"range": [round(lo, 2), round(hi, 2)], "count": 0, "avg_confidence": None, "accuracy": None})
            continue
        avg_conf = sum(c for c, _ in members) / len(members)
        acc = sum(1 for _, ok in members if ok) / len(members)
        ece += (len(members) / n) * abs(avg_conf - acc)
        buckets.append({
            "range": [round(lo, 2), round(hi, 2)],
            "count": len(members),
            "avg_confidence": round(avg_conf, 3),
            "accuracy": round(acc, 3),
        })

    return {"n": n, "ece": round(ece, 4), "brier": round(brier, 4), "buckets": buckets}
