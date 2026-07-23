"""
Gunicorn config.

One worker with many threads: the workload is I/O-bound (LLM + tool calls) and
the orchestrator keeps shared in-process state (SSE subscribers, HITL queue,
rate-limiter buckets). Multiple processes would fragment that state, so we
scale with threads inside a single worker instead.
"""

bind = "0.0.0.0:5000"
workers = 1
threads = 16
worker_class = "gthread"
timeout = 300          # long-running SSE / batch processing
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
