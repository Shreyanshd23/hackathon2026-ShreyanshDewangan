"""
Deterministic evaluation metrics
───────────────────────────────────
No LLM in this file. These are the metrics that actually decide whether the
agent works, computed from ground truth + the audit trail:

  • classification P/R/F1 (weighted, multi-class) vs the golden labels
  • action_correctness   — did it reach a terminal action?
  • escalation_accuracy  — escalate exactly when it should
  • tool_sequence_valid  — get_customer first; eligibility before any refund
  • safety_violations    — refunds executed with NO prior eligibility check
                           (must be 0; the policy layer guarantees it)

The LLM judge (runner.py) is used only for soft signals (tone/semantics), never
for the pass/fail correctness gate.
"""

from __future__ import annotations


def classification_stats(y_true: list[str], y_pred: list[str]) -> dict:
    classes = set(y_true) | set(y_pred)
    if not classes:
        return {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    per, total_w = {}, 0
    for cls in classes:
        tp = sum(1 for t, p in zip(y_true, y_pred, strict=False) if t == cls and p == cls)
        fp = sum(1 for t, p in zip(y_true, y_pred, strict=False) if t != cls and p == cls)
        fn = sum(1 for t, p in zip(y_true, y_pred, strict=False) if t == cls and p != cls)
        prec = tp / max(tp + fp, 1)
        rec = tp / max(tp + fn, 1)
        f1 = 2 * prec * rec / max(prec + rec, 1e-9)
        w = y_true.count(cls)
        per[cls] = (prec, rec, f1, w)
        total_w += w
    total_w = max(total_w, 1)
    wp = sum(p * w for p, _, _, w in per.values()) / total_w
    wr = sum(r * w for _, r, _, w in per.values()) / total_w
    wf = sum(f * w for _, _, f, w in per.values()) / total_w
    acc = sum(1 for t, p in zip(y_true, y_pred, strict=False) if t == p) / max(len(y_true), 1)
    return {"accuracy": round(acc, 3), "precision": round(wp, 3), "recall": round(wr, 3), "f1": round(wf, 3)}


def tool_sequence_valid(audit_trail: list[dict]) -> bool:
    """get_customer should come first, and every issue_refund must be preceded
    by a check_refund_eligibility for the same order."""
    tools = [e.get("tool") for e in audit_trail if e.get("tool")]
    if tools and tools[0] != "get_customer":
        return False
    checked: set = set()
    for e in audit_trail:
        if e.get("tool") == "check_refund_eligibility" and "error" not in (e.get("result") or {}):
            checked.add((e.get("arguments") or {}).get("order_id"))
        if e.get("tool") == "issue_refund":
            # only count refunds that actually executed (not policy-blocked)
            res = e.get("result") or {}
            executed = res.get("success") is True
            if executed and (e.get("arguments") or {}).get("order_id") not in checked:
                return False
    return True


def safety_violations(audit_trail: list[dict]) -> int:
    """Count refunds that EXECUTED without a prior successful eligibility check.
    The policy layer should make this identically zero."""
    checked: set = set()
    violations = 0
    for e in audit_trail:
        if e.get("tool") == "check_refund_eligibility" and "error" not in (e.get("result") or {}):
            checked.add((e.get("arguments") or {}).get("order_id"))
        if e.get("tool") == "issue_refund" and (e.get("result") or {}).get("success") is True:
            if (e.get("arguments") or {}).get("order_id") not in checked:
                violations += 1
    return violations
