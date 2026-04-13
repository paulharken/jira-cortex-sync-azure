"""Azure Function App entrypoint for Cortex <-> Jira Sync."""

import json
import logging

import azure.functions as func

from sync.config import Config
from sync.engine import run_sync, test_connectivity
from sync.state import get_container_client, get_state
from sync.log import get_logger

app = func.FunctionApp()
logger = get_logger()


# ---------------------------------------------------------------------------
# Timer trigger: runs the sync cycle every 60 seconds
# ---------------------------------------------------------------------------

@app.timer_trigger(
    schedule="0 */1 * * * *",
    arg_name="timer",
    run_on_startup=False,
)
def sync_timer(timer: func.TimerRequest) -> None:
    """Scheduled sync cycle — runs every 60 seconds."""
    if timer.past_due:
        logger.warning("Timer trigger is past due — running catch-up sync")

    try:
        config = Config.from_env()
        container = get_container_client()
        results = run_sync(config, container)
        logger.info(f"Sync completed: {results.get('summary', 'no summary')}")
    except Exception:
        logger.exception("Sync cycle failed")


# ---------------------------------------------------------------------------
# HTTP trigger: health check / status endpoint
# ---------------------------------------------------------------------------

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Returns current sync status and state summary."""
    try:
        config = Config.from_env()
        errors = config.validate()
        if errors:
            return func.HttpResponse(
                json.dumps({"status": "misconfigured", "errors": errors}),
                status_code=500,
                mimetype="application/json",
            )

        container = get_container_client()
        state = get_state(container)

        open_cases = sum(1 for r in state["sync_records"].values() if r["status"] == "open")
        closed_cases = sum(1 for r in state["sync_records"].values() if r["status"] == "closed")
        open_issues = sum(1 for r in state["issue_sync_records"].values() if r["status"] == "open")
        closed_issues = sum(1 for r in state["issue_sync_records"].values() if r["status"] == "closed")

        status = {
            "status": "ok",
            "last_cortex_poll_ms": state.get("last_poll_ms", 0),
            "last_jira_poll_iso": state.get("last_jira_poll_iso", "never"),
            "open_cases": open_cases,
            "closed_cases": closed_cases,
            "open_issues": open_issues,
            "closed_issues": closed_issues,
            "retry_queue_size": len(state.get("retry_queue", [])),
            "cached_users": len(state.get("user_cache", {})),
        }

        return func.HttpResponse(
            json.dumps(status, indent=2),
            mimetype="application/json",
        )
    except Exception as e:
        logger.exception("Health check failed")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=500,
            mimetype="application/json",
        )


# ---------------------------------------------------------------------------
# HTTP trigger: manual sync
# ---------------------------------------------------------------------------

@app.route(route="sync", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def manual_sync(req: func.HttpRequest) -> func.HttpResponse:
    """Trigger a sync cycle on demand."""
    try:
        config = Config.from_env()
        container = get_container_client()
        results = run_sync(config, container)

        return func.HttpResponse(
            json.dumps(results, indent=2, default=str),
            mimetype="application/json",
        )
    except Exception as e:
        logger.exception("Manual sync failed")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=500,
            mimetype="application/json",
        )


# ---------------------------------------------------------------------------
# HTTP trigger: connectivity test
# ---------------------------------------------------------------------------

@app.route(route="test", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def connectivity_test(req: func.HttpRequest) -> func.HttpResponse:
    """Test connectivity to Cortex and Jira."""
    try:
        config = Config.from_env()
        result = test_connectivity(config)
        status_code = 200 if result.get("status") == "ok" else 500

        return func.HttpResponse(
            json.dumps(result, indent=2),
            status_code=status_code,
            mimetype="application/json",
        )
    except Exception as e:
        logger.exception("Connectivity test failed")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
