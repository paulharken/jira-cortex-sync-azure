"""State persistence via Azure Blob Storage."""

import json
import os
from datetime import datetime, timedelta, timezone

from azure.storage.blob import ContainerClient

from .config import CLOSED_RECORD_TTL_DAYS
from .log import get_logger

logger = get_logger()

BLOB_NAME = "state.json"

_DEFAULT_STATE = {
    "last_poll_ms": 0,
    "last_jira_poll_iso": "",
    "sync_records": {},
    "issue_sync_records": {},
    "retry_queue": [],
    "user_cache": {},
}


def get_container_client() -> ContainerClient:
    """Create a ContainerClient from environment variables."""
    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    container_name = os.environ.get("STATE_CONTAINER_NAME", "cortex-jira-sync")
    client = ContainerClient.from_connection_string(conn_str, container_name)
    # Ensure the container exists
    try:
        client.get_container_properties()
    except Exception:
        client.create_container()
        logger.info(f"Created blob container: {container_name}")
    return client


def get_state(container: ContainerClient) -> dict:
    """Load persistent state from Azure Blob Storage."""
    try:
        blob = container.download_blob(BLOB_NAME)
        state = json.loads(blob.readall())
    except Exception:
        logger.info("No existing state blob found — starting fresh")
        state = {}

    # Ensure all expected keys exist
    for key, default in _DEFAULT_STATE.items():
        state.setdefault(key, default if not isinstance(default, (dict, list)) else type(default)())
    return state


def save_state(container: ContainerClient, state: dict) -> None:
    """Persist state to Azure Blob Storage."""
    data = json.dumps(state, default=str)
    container.upload_blob(BLOB_NAME, data, overwrite=True)


def prune_closed_records(state: dict) -> int:
    """Remove closed records older than CLOSED_RECORD_TTL_DAYS. Returns count pruned."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=CLOSED_RECORD_TTL_DAYS)
    cutoff_iso = cutoff.isoformat()
    pruned = 0

    for records_key in ("sync_records", "issue_sync_records"):
        to_delete = []
        for record_id, record in state[records_key].items():
            if record.get("status") == "closed":
                closed_at = record.get("closed_at", record.get("created_at", ""))
                if closed_at and closed_at < cutoff_iso:
                    to_delete.append(record_id)
        for record_id in to_delete:
            del state[records_key][record_id]
            pruned += 1

    return pruned
