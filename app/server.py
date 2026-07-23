"""
Flask server (hardened)
─────────────────────────
Changes vs the original:
  • debug is OFF by default (was hardcoded True → Werkzeug debugger = RCE).
    Enable only via FLASK_DEBUG=1 for local dev.
  • CORS is restricted to an explicit allow-list (was a wildcard).
  • New endpoints expose the production surfaces: HITL approvals, dead-letter
    queue, health, and calibration.

Endpoints:
  GET  /                → dashboard
  POST /api/process     → start processing all tickets
  POST /api/evaluate    → run the offline eval harness over completed tickets
  GET  /api/stream      → SSE event stream
  GET  /api/results     → completed results (+ evaluations merged)
  GET  /api/status      → running flag + counts
  GET  /api/hitl        → actions awaiting human approval
  GET  /api/dlq         → dead-letter queue (failed tickets)
  GET  /api/analytics   → scorecard + calibration
"""

from __future__ import annotations

import json
import os
import queue
import threading

from flask import Flask, Response, jsonify, render_template
from flask_cors import CORS

from app.orchestrator import orchestrator
from core.config import settings

app = Flask(__name__,
            template_folder=os.path.join(settings.data_dir, "templates"),
            static_folder=os.path.join(settings.data_dir, "static"))
CORS(app, origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()])

_evaluations: dict = {}
_eval_lock = threading.Lock()


@app.route("/")
def index():
    with open(os.path.join(settings.data_dir, "tickets.json"), encoding="utf-8") as f:
        tickets = json.load(f)
    return render_template("index.html", tickets_json=json.dumps(tickets))


@app.route("/api/process", methods=["POST"])
def process_tickets():
    if orchestrator.is_running():
        return jsonify({"error": "Processing already in progress"}), 409
    threading.Thread(target=orchestrator.run_all, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/evaluate", methods=["POST"])
def evaluate_tickets():
    results = orchestrator.get_results()
    if not results:
        return jsonify({"error": "No tickets processed yet"}), 404

    def _run():
        from evals.runner import evaluate_results
        report = evaluate_results(results, on_event=orchestrator.broadcast)
        with _eval_lock:
            _evaluations.clear()
            _evaluations.update(report.get("evaluations", {}))
        orchestrator.broadcast({"type": "eval_complete", "scorecard": report.get("scorecard", {})})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/stream")
def stream():
    q: queue.Queue = queue.Queue()
    orchestrator.subscribe(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(data, default=str)}\n\n"
        finally:
            orchestrator.unsubscribe(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/results")
def results():
    res = orchestrator.get_results()
    with _eval_lock:
        for r in res:
            if r["ticket_id"] in _evaluations:
                r["evaluation"] = _evaluations[r["ticket_id"]]
    return jsonify(res)


@app.route("/api/status")
def status():
    return jsonify({"running": orchestrator.is_running(), "completed": len(orchestrator.get_results())})


@app.route("/api/hitl")
def hitl():
    return jsonify(orchestrator.engine.hitl.all())


@app.route("/api/dlq")
def dlq():
    return jsonify(orchestrator.store.dlq())


@app.route("/api/analytics")
def analytics():
    from evals.runner import build_scorecard
    results = orchestrator.get_results()
    with _eval_lock:
        evals = dict(_evaluations)
    if not results:
        return jsonify({"error": "No analytics available"}), 404
    return jsonify(build_scorecard(results, evals))


def main():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")),
            debug=settings.flask_debug, threaded=True)


if __name__ == "__main__":
    main()
