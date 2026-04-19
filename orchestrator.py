"""
Ticket Orchestrator
───────────────────
Loads all 20 tickets, runs the agent pipeline concurrently,
and streams progress events to subscribers (the Flask SSE endpoint).
"""

import json, os, time, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from config import DATA_DIR, CONCURRENT_TICKETS
from agents import ReaderAgent, ClassifierAgent, ResolverAgent

# ── Global state ─────────────────────────────────────────────
_results = []           # completed ticket results
_eval_results = {}      # ticket_id -> gemini evaluation
_is_running = False
_is_evaluating = False
_subscribers = []       # list of queue.Queue for SSE


def subscribe(q):
    _subscribers.append(q)

def unsubscribe(q):
    if q in _subscribers:
        _subscribers.remove(q)

def broadcast(event: dict):
    """Push an event to every SSE subscriber."""
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except Exception:
            pass


def get_results():
    return list(_results)

def get_evaluations():
    return dict(_eval_results)


def is_running():
    return _is_running

def is_evaluating():
    return _is_evaluating


# ── Load tickets ─────────────────────────────────────────────
def _load_tickets():
    path = os.path.join(DATA_DIR, "tickets.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Process a single ticket (runs in thread) ─────────────────
def _process_one(ticket: dict) -> dict:
    tid = ticket["ticket_id"]
    t0  = time.time()

    try:
        # ── Stage 1: Reader ────────────────────────────
        broadcast({"type": "agent_update", "ticket_id": tid,
                    "agent": "reader", "status": "running"})
        parsed = ReaderAgent.run(ticket)
        broadcast({"type": "agent_update", "ticket_id": tid,
                    "agent": "reader", "status": "complete",
                    "data": {
                        "extracted_order_ids": parsed["extracted_order_ids"],
                        "has_threatening_language": parsed["has_threatening_language"],
                        "has_urgency_signals": parsed["has_urgency_signals"],
                    }})

        # ── Stage 2: Classifier ────────────────────────
        broadcast({"type": "agent_update", "ticket_id": tid,
                    "agent": "classifier", "status": "running"})
        classification = ClassifierAgent.run(parsed)
        broadcast({"type": "agent_update", "ticket_id": tid,
                    "agent": "classifier", "status": "complete",
                    "data": classification})

        # ── Stage 3: Resolver ──────────────────────────
        broadcast({"type": "agent_update", "ticket_id": tid,
                    "agent": "resolver", "status": "running"})

        def on_tool_call(entry):
            broadcast({"type": "tool_call", "ticket_id": tid, **entry})

        resolution = ResolverAgent.run(parsed, classification, callback=on_tool_call)
        broadcast({"type": "agent_update", "ticket_id": tid,
                    "agent": "resolver", "status": "complete"})

        elapsed = round(time.time() - t0, 2)

        result = {
            "ticket_id":      tid,
            "subject":        ticket.get("subject", ""),
            "customer_email": ticket.get("customer_email", ""),
            "body":           ticket.get("body", ""),
            "expected_action": ticket.get("expected_action", ""),
            "parsed":         parsed,
            "classification": classification,
            "resolution":     resolution,
            "status":         resolution["status"],
            "elapsed_seconds": elapsed,
            "timestamp":      datetime.now().isoformat(),
        }

        broadcast({"type": "ticket_done", "ticket_id": tid,
                    "status": resolution["status"], "elapsed": elapsed,
                    "classification": classification,
                    "tool_calls": resolution["tool_calls"]})
        return result

    except Exception as exc:
        elapsed = round(time.time() - t0, 2)
        tb = traceback.format_exc()
        result = {
            "ticket_id":      tid,
            "subject":        ticket.get("subject", ""),
            "customer_email": ticket.get("customer_email", ""),
            "body":           ticket.get("body", ""),
            "expected_action": ticket.get("expected_action", ""),
            "status":         "failed",
            "error":          str(exc),
            "traceback":      tb,
            "elapsed_seconds": elapsed,
            "timestamp":      datetime.now().isoformat(),
        }
        broadcast({"type": "ticket_done", "ticket_id": tid,
                    "status": "failed", "elapsed": elapsed,
                    "error": str(exc)})
        return result


# ── Run all tickets concurrently ─────────────────────────────
def run_all():
    global _results, _is_running
    if _is_running:
        return {"error": "Already running"}

    _is_running = True
    _results.clear()

    tickets = _load_tickets()
    broadcast({"type": "start", "total": len(tickets)})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        for t in tickets:
            futures[pool.submit(_process_one, t)] = t["ticket_id"]
            if len(futures) < len(tickets): 
                time.sleep(10) # Safe buffer for Groq TPM 12,000
        
        for future in as_completed(futures):
            result = future.result()
            _results.append(result)

    # Compute summary stats
    resolved  = sum(1 for r in _results if r["status"] == "resolved")
    escalated = sum(1 for r in _results if r["status"] == "escalated")
    failed    = sum(1 for r in _results if r["status"] == "failed")
    incomplete= sum(1 for r in _results if r["status"] == "incomplete")
    avg_time  = round(sum(r.get("elapsed_seconds", 0) for r in _results) / max(len(_results), 1), 2)
    total_tools = sum(r.get("resolution", {}).get("tool_calls", 0) for r in _results)

    stats = {
        "total":      len(_results),
        "resolved":   resolved,
        "escalated":  escalated,
        "failed":     failed,
        "incomplete": incomplete,
        "avg_time":   avg_time,
        "total_tool_calls": total_tools,
    }

    broadcast({"type": "complete", "stats": stats})
    
    # Save Audit Log for Submission
    try:
        with open("audit_log.json", "w", encoding="utf-8") as f:
            json.dump(_results, f, indent=2)
        print("✅ Audit Log saved to audit_log.json")
    except Exception as e:
        print(f"Failed to save audit log: {e}")

    _is_running = False
    return stats


def run_evaluation():
    """Evaluate all completed tickets using Gemini."""
    global _eval_results, _is_evaluating
    if _is_evaluating:
        return {"error": "Evaluation already in progress"}
    
    if not _results:
        return {"error": "No tickets have been processed yet"}

    from evaluate import Evaluator
    _is_evaluating = True
    _eval_results.clear()
    
    broadcast({"type": "eval_start", "total": len(_results)})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for r in _results:
                futures[pool.submit(Evaluator.evaluate_ticket, r)] = r["ticket_id"]
                time.sleep(3) 
            
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    eval_data = future.result()
                    _eval_results[tid] = eval_data
                    broadcast({"type": "eval_done", "ticket_id": tid, "data": eval_data})
                except Exception as e:
                    print(f"Eval failed for {tid}: {e}")

        # Calculate scorecard
        scorecard = Evaluator.calculate_scorecard(_results, _eval_results)
        broadcast({"type": "eval_complete", "scorecard": scorecard})
    except Exception as e:
        print(f"Global Eval Error: {e}")
        broadcast({"type": "eval_error", "error": str(e)})
    finally:
        _is_evaluating = False
    
    return scorecard if '_eval_results' in locals() else {}
