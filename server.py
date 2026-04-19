"""
ShopWave AI Support – Flask Server
───────────────────────────────────
Endpoints:
  GET  /                → Web dashboard
  POST /api/process     → Start processing all tickets
  GET  /api/stream      → SSE real-time event stream
  GET  /api/results     → Fetch completed results
  GET  /api/status      → Quick status check
"""

import json, os, queue, threading
from flask import Flask, render_template, jsonify, Response, request
from flask_cors import CORS
import orchestrator
from config import DATA_DIR

app = Flask(__name__,
            template_folder="templates",
            static_folder="static")
CORS(app)

# ──────────────────────────────────────────────────────────
#  Pages
# ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    tickets_path = os.path.join(DATA_DIR, "tickets.json")
    with open(tickets_path, encoding="utf-8") as f:
        tickets = json.load(f)
    return render_template("index.html", tickets_json=json.dumps(tickets))

# ──────────────────────────────────────────────────────────
#  API — kick off processing
# ──────────────────────────────────────────────────────────
@app.route("/api/process", methods=["POST"])
def process_tickets():
    if orchestrator.is_running():
        return jsonify({"error": "Processing already in progress"}), 409
    thread = threading.Thread(target=orchestrator.run_all, daemon=True)
    thread.start()
    return jsonify({"status": "started"})

# ──────────────────────────────────────────────────────────
#  API — evaluation
# ──────────────────────────────────────────────────────────
@app.route("/api/evaluate", methods=["POST"])
def evaluate_tickets():
    if orchestrator.is_evaluating():
        return jsonify({"error": "Evaluation already in progress"}), 409
    thread = threading.Thread(target=orchestrator.run_evaluation, daemon=True)
    thread.start()
    return jsonify({"status": "started"})

@app.route("/api/analytics")
def analytics():
    # Only calculate if we have results
    from evaluate import Evaluator
    results = orchestrator.get_results()
    evals = orchestrator.get_evaluations()
    if not results or not evals:
        return jsonify({"error": "No analytics available"}), 404
    scorecard = Evaluator.calculate_scorecard(results, evals)
    return jsonify(scorecard)

# ──────────────────────────────────────────────────────────
#  API — SSE stream
# ──────────────────────────────────────────────────────────
@app.route("/api/stream")
def stream():
    q = queue.Queue()
    orchestrator.subscribe(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=60)
                except queue.Empty:
                    # Send keep-alive
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(data)}\n\n"
                # Don't break on complete anymore, wait for evaluation too? 
                # Actually browser will reconnect if it needs to.
        finally:
            orchestrator.unsubscribe(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

# ──────────────────────────────────────────────────────────
#  API — fetch results
# ──────────────────────────────────────────────────────────
@app.route("/api/results")
def results():
    res = orchestrator.get_results()
    evals = orchestrator.get_evaluations()
    # Merge evals into results for the frontend
    for r in res:
        if r["ticket_id"] in evals:
            r["evaluation"] = evals[r["ticket_id"]]
    return jsonify(res)

@app.route("/api/status")
def status():
    return jsonify({"running": orchestrator.is_running(),
                    "completed": len(orchestrator.get_results())})

# ──────────────────────────────────────────────────────────
#  Run
# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
