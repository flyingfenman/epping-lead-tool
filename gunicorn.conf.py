workers = 1
timeout = 120
bind = "0.0.0.0:8080"

def post_fork(server, worker):
    # Schedulers must be started after the worker fork, not at import time.
    # Importing here is safe — the module is already cached, this just restarts the schedulers.
    from app import scheduler
    from sales import sales_scheduler
    if not scheduler.running:
        scheduler.start()
    if not sales_scheduler.running:
        sales_scheduler.start()
