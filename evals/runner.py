"""
Evaluation harness + CI gate
──────────────────────────────
Runs the whole pipeline over the golden set (offline, no keys) and produces an
honest scorecard. Unlike the original — which scored against the hackathon's
own rubric with several sub-scores hardcoded — correctness here comes from
ground truth and deterministic checks. The LLM judge contributes only soft
signals (tone/semantics).

Usage:
    python -m evals.runner            # run + print scorecard
    python -m evals.runner --gate     # also exit non-zero if thresholds fail (CI)
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

from core import llm_client
from evals.metrics import classification_stats, safety_violations, tool_sequence_valid
from monitoring import calibration_report

_GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden.json")

# CI gate thresholds.
# The HARD gates are the invariants WE engineered and fully control — these
# must hold regardless of model quality:
#   • zero safety violations (a refund never executes without eligibility)
#   • the agent reaches a terminal action
#   • the tool sequence is valid (customer-first, eligibility-before-refund)
# Classification F1 is a MODEL-quality metric. In CI it runs against the
# deterministic keyword *mock* (F1 ≈ 0.53), so we gate only on a regression
# floor; the production Llama-70B target is ≥0.85 (tracked, not gated here).
GATE = {
    "min_action_correctness": 0.80,
    "min_tool_sequence_validity": 0.90,
    "max_safety_violations": 0,
    "min_f1_regression_floor": 0.45,
}


def _golden() -> dict[str, dict]:
    with open(_GOLDEN_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {row["ticket_id"]: row for row in data["labels"]}


def _judge_prompt(result: dict) -> str:
    res = result.get("resolution") or {}
    return f"""Evaluate an AI support agent's handling of ticket {result['ticket_id']}.
Subject: {result.get('subject')}
Body: {result.get('body')}
Expected outcome: {result.get('expected_action')}
Agent status: {result.get('status')}
Final reply: {res.get('final_text')}
Audit trail: {json.dumps(res.get('audit_trail', []))[:2000]}

Return JSON with keys: true_category, is_correct (bool), semantic_similarity (0-1),
logic_score (0-1), tone_score (0-1), feedback."""


def evaluate_results(results: list[dict], on_event: Callable[[dict], None] | None = None) -> dict:
    """Judge each ticket (soft signals) and build the scorecard."""
    evaluations: dict[str, dict] = {}
    for r in results:
        tid = r["ticket_id"]
        if on_event:
            on_event({"type": "eval_start", "ticket_id": tid})
        ev = llm_client.gemini_evaluate(_judge_prompt(r))
        evaluations[tid] = ev
        if on_event:
            on_event({"type": "eval_done", "ticket_id": tid, "data": ev})
    return {"evaluations": evaluations, "scorecard": build_scorecard(results, evaluations)}


def build_scorecard(results: list[dict], evaluations: dict) -> dict:
    golden = _golden()
    n = len(results)
    if n == 0:
        return {}

    y_true, y_pred, calib_pairs = [], [], []
    terminal = escalation_ok = escalation_total = seq_ok = 0
    total_safety = 0

    for r in results:
        tid = r["ticket_id"]
        g = golden.get(tid, {})
        res = r.get("resolution") or {}
        cls = r.get("classification") or {}
        pred_cat = cls.get("category", "ambiguous")

        if "true_category" in g:
            y_true.append(g["true_category"])
            y_pred.append(pred_cat)
            calib_pairs.append((float(res.get("self_confidence", 0.5)), pred_cat == g["true_category"]))

        if r.get("status") in ("resolved", "escalated"):
            terminal += 1

        exp_esc = g.get("expected_escalation")
        if exp_esc is not None:
            escalation_total += 1
            if (r.get("status") == "escalated") == bool(exp_esc):
                escalation_ok += 1

        audit = res.get("audit_trail", [])
        if tool_sequence_valid(audit):
            seq_ok += 1
        total_safety += safety_violations(audit)

    cls_stats = classification_stats(y_true, y_pred)
    scorecard = {
        "n": n,
        "classification": cls_stats,
        "action_correctness": round(terminal / n, 3),
        "escalation_accuracy": round(escalation_ok / escalation_total, 3) if escalation_total else None,
        "tool_sequence_validity": round(seq_ok / n, 3),
        "safety_violations": total_safety,
        "calibration": calibration_report(calib_pairs),
        "avg_self_confidence": round(sum((r.get("resolution") or {}).get("self_confidence", 0) for r in results) / n, 3),
        "degraded_tickets": sum(1 for r in results if r.get("health") == "degraded"),
        "judge_mean_semantic_similarity": round(
            sum(e.get("semantic_similarity", 0) for e in evaluations.values()) / max(len(evaluations), 1), 3
        ) if evaluations else None,
    }
    scorecard["gate_pass"] = (
        scorecard["action_correctness"] >= GATE["min_action_correctness"]
        and scorecard["tool_sequence_validity"] >= GATE["min_tool_sequence_validity"]
        and total_safety <= GATE["max_safety_violations"]
        and cls_stats["f1"] >= GATE["min_f1_regression_floor"]
    )
    return scorecard


def _run_pipeline_offline() -> list[dict]:
    os.environ["OFFLINE_MODE"] = "1"
    os.environ.setdefault("FAILURE_INJECTION_RATE", "0.0")
    import tools.impls as ti
    ti.FAILURE_INJECTION_RATE = 0.0
    from app.orchestrator import Orchestrator
    orch = Orchestrator()
    orch.run_all()
    return orch.get_results()


def main() -> int:
    results = _run_pipeline_offline()
    report = evaluate_results(results)
    scorecard = report["scorecard"]
    print(json.dumps(scorecard, indent=2))
    if "--gate" in sys.argv:
        if not scorecard.get("gate_pass"):
            print("\n❌ EVAL GATE FAILED", file=sys.stderr)
            return 1
        print("\n✅ EVAL GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
