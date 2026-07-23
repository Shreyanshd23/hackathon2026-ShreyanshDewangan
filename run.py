"""
Single entry point.

    python run.py                 # start the web dashboard (Flask dev server)
    python run.py --headless      # process all tickets once, write audit_log.json, exit
    python run.py --eval          # headless run + scored evaluation (offline)

For production the WSGI app is `app.server:app` (see docker/gunicorn.conf.py).
"""

from __future__ import annotations

import json
import sys


def _headless(with_eval: bool) -> int:
    from app.orchestrator import Orchestrator
    orch = Orchestrator()
    stats = orch.run_all()
    print(json.dumps({"run_stats": stats}, indent=2, default=str))
    if with_eval:
        from evals.runner import evaluate_results
        report = evaluate_results(orch.get_results())
        print(json.dumps({"scorecard": report["scorecard"]}, indent=2, default=str))
    return 0


def main() -> int:
    if "--headless" in sys.argv or "--eval" in sys.argv:
        return _headless(with_eval="--eval" in sys.argv)
    from app.server import main as serve
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
