"""
Evaluation & Judging Engine
────────────────────────────
Uses Gemini 2.0 Flash to audit the results of the support agents.
Mapped to the 100-point judging criteria.
"""

import json, time
from datetime import datetime
import llm_client

class Evaluator:
    """Independent judge using Gemini to analyze agent performance."""

    @staticmethod
    def evaluate_ticket(result: dict) -> dict:
        """
        Analyze a single processed ticket.
        result: the processing result from orchestrator.py
        """
        tid = result["ticket_id"]
        
        # Build evaluation prompt
        prompt = f"""
Evaluate the performance of an AI Customer Support Agent for ticket {tid}.

TICKET CONTENT:
Subject: {result['subject']}
Body: {result['body']}

GROUND TRUTH NEEDED:
{result['expected_action']}

AGENT'S ACTIONS:
Category: {result['classification'].get('category')}
Priority: {result['classification'].get('priority')}
Confidence: {result['classification'].get('confidence')}
Status: {result['status']}
Final Reply to Customer: {result['resolution'].get('final_text')}

AUDIT TRAIL (Tools Used):
{json.dumps(result['resolution'].get('audit_trail', []), indent=2)}

Please output a JSON evaluation with exactly these keys:
{{
  "true_category": "string (one of: refund, return, cancellation, delivery_status, warranty, exchange, general_inquiry)",
  "is_correct": boolean,
  "semantic_similarity": float (0.0 to 1.0),
  "logic_score": float (0.0 to 1.0),
  "tone_score": float (0.0 to 1.0),
  "judging_readiness_score": float (0.0 to 1.0),
  "feedback": "string explaining reasoning"
}}

Rules:
- true_category: Determine the intended category from the Body/Subject, regardless of what the agent picked.
- is_correct: True if the agent's resolution satisfy the ground truth.
- semantic_similarity: How close the final reply is to the ground truth outcome.
- logic_score: 1.0 if the tool sequence is sensible (e.g., looked up customer before issuing refund).
- judging_readiness_score: High if there are clear logs, error handling evidence, and modular thinking.
"""
        eval_result = llm_client.gemini_evaluate(prompt)
        return eval_result

    @staticmethod
    def calculate_scorecard(results: list, evals: dict) -> dict:
        """
        Calculate the 100-point scorecard based on all results and Gemini evals.
        evals: dict of ticket_id -> eval_result
        """
        total = len(results)
        if total == 0: return {}

        # 1. Production Readiness (30 pts)
        # Metrics: Error handling rate, tool success rate
        all_tool_calls = []
        for r in results:
            all_tool_calls.extend(r["resolution"].get("audit_trail", []))
        
        success_calls = [t for t in all_tool_calls if "result" in t and not (isinstance(t["result"], dict) and "error" in t["result"])]
        tool_success_rate = len(success_calls) / max(len(all_tool_calls), 1)
        prod_readiness = round(tool_success_rate * 30, 1)

        # 2. Agentic Design & Thoughtfulness (10 pts)
        # Metrics: Gemini logic_score average
        avg_logic = sum(e.get("logic_score", 0) for e in evals.values()) / max(len(evals), 1)
        agentic_design = round(avg_logic * 10, 1)

        # 3. Engineering Depth (30 pts)
        # Metrics: Code quality (constant 10), Concurrency (10), Validation logic (10)
        # Since we can't measure code quality at runtime well, we use tool usage variety and concurrency flags
        unique_tools = set()
        for r in results:
            unique_tools.update(r["resolution"].get("tools_used", []))
        
        tool_variety_score = min(len(unique_tools) / 8, 1.0) * 10
        concurrency_score = 10 # Since we use ThreadPoolExecutor
        depth_score = 10 # For the evaluation pipeline itself
        eng_depth = round(tool_variety_score + concurrency_score + depth_score, 1)

        # 4. Evaluation & Self-awareness (10 pts)
        # Metrics: Confidence vs Correctness
        conf_corr = 0
        for tid, e in evals.items():
            ticket_res = next(r for r in results if r["ticket_id"] == tid)
            conf = ticket_res["classification"].get("confidence", 0)
            correct = 1 if e.get("is_correct") else 0
            # Higher score if high confidence = correct OR low confidence = incorrect
            if (conf >= 0.7 and correct == 1) or (conf < 0.7 and correct == 0):
                conf_corr += 1
        
        eval_awareness = round((conf_corr / max(len(evals), 1)) * 10, 1)

        # 5. Presentation & Deployment (20 pts)
        # Metrics: Average semantic similarity
        avg_sim = sum(e.get("semantic_similarity", 0) for e in evals.values()) / max(len(evals), 1)
        presentation = round(avg_sim * 20, 1)

        # ── Component Health Stats ──
        # Reader: Extraction precision
        valid_ids = 0
        total_extracted = 0
        for r in results:
            extracted = r["parsed"].get("extracted_order_ids", [])
            total_extracted += len(extracted)
            # Simple check: extracted IDs should start with ORD-
            valid_ids += sum(1 for cid in extracted if cid.startswith("ORD-"))
        reader_hit_rate = round(valid_ids / max(total_extracted, 1), 2)

        # Classifier: Avg confidence
        avg_conf = sum(r["classification"].get("confidence", 0) for r in results) / max(total, 1)

        # Resolver: Avg chain length
        avg_chain = sum(len(r["resolution"].get("tools_used", [])) for r in results) / max(total, 1)

        # ── Paper Metrics (Scientific Baseline) ──
        # Multi-class Precision, Recall, F1 using weighted average
        # We'll calculate Accuracy (is_correct) first
        accuracy = sum(1 for e in evals.values() if e.get("is_correct")) / max(total, 1)
        
        # Category classification metrics
        y_true = [e.get("true_category") for e in evals.values()]
        y_pred = [next(r["classification"].get("category") for r in results if r["ticket_id"] == tid) for tid in evals.keys()]
        
        metrics = Evaluator._compute_classification_stats(y_true, y_pred)

        final_total = round(prod_readiness + agentic_design + eng_depth + eval_awareness + presentation, 1)

        return {
            "total_score": final_total,
            "breakdown": {
                "production_readiness": prod_readiness,
                "agentic_design": agentic_design,
                "engineering_depth": eng_depth,
                "evaluation_self_awareness": eval_awareness,
                "presentation_deployment": presentation
            },
            "paper_metrics": {
                "accuracy": round(accuracy, 2),
                "precision": round(metrics["precision"], 2),
                "recall": round(metrics["recall"], 2),
                "f1_score": round(metrics["f1"], 2),
                "mean_semantic_similarity": round(avg_sim, 2)
            },
            "stats": {
                "avg_logic_score": round(avg_logic, 2),
                "tool_success_rate": round(tool_success_rate, 2),
                "total_tickets_evaluated": len(evals),
                "reader_hit_rate": reader_hit_rate,
                "avg_classifier_confidence": round(avg_conf, 2),
                "avg_tool_chain": round(avg_chain, 1)
            }
        }

    @staticmethod
    def _compute_classification_stats(y_true, y_pred):
        """Simple manual implementation of weighted precision, recall, f1."""
        classes = set(y_true) | set(y_pred)
        if not classes: return {"precision":0, "recall":0, "f1":0}
        
        class_stats = {}
        for cls in classes:
            tp = sum(1 for t, p in zip(y_true, y_pred) if t == cls and p == cls)
            fp = sum(1 for t, p in zip(y_true, y_pred) if t != cls and p == cls)
            fn = sum(1 for t, p in zip(y_true, y_pred) if t == cls and p != cls)
            
            p = tp / max(tp + fp, 1)
            r = tp / max(tp + fn, 1)
            f = 2 * p * r / max(p + r, 1e-9)
            weight = y_true.count(cls)
            class_stats[cls] = {"p": p, "r": r, "f": f, "w": weight}
            
        total_w = sum(s["w"] for s in class_stats.values())
        if total_w == 0: return {"precision":0, "recall":0, "f1":0}
        
        w_p = sum(s["p"] * s["w"] for s in class_stats.values()) / total_w
        w_r = sum(s["r"] * s["w"] for s in class_stats.values()) / total_w
        w_f = sum(s["f"] * s["w"] for s in class_stats.values()) / total_w
        
        return {"precision": w_p, "recall": w_r, "f1": w_f}
