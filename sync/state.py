"""State persistence via Azure Blob Storage with ETag-based optimistic concurrency."""

import json
import os
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContainerClient

from .config import CLOSED_RECORD_TTL_DAYS
from .log import get_logger

logger = get_logger()

BLOB_NAME = "state.json"
ETAG_KEY = "_etag"

_DEFAULT_STATE = {
    "last_poll_ms": 0,
    "last_jira_poll_iso": "",
    "sync_records": {},
    "issue_sync_records": {},
    "retry_queue": [],
    "user_cache": {},
}


class StateConflictError(Exception):
    """Raised when save_state detects a concurrent write (ETag mismatch)."""
    pass


def get_container_client() -> ContainerClient:
    """Create a ContainerClient from environment variables."""
    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    container_name = os.environ.get("STATE_CONTAINER_NAME", "cortex-jira-sync")
    client = ContainerClient.from_connection_string(conn_str, container_name)
    # Ensure the container exists
    try:
        client.get_container_properties()
    except ResourceNotFoundError:
        client.create_container()
        logger.info(f"Created blob container: {container_name}")
    return client


def get_state(container: ContainerClient) -> dict:
    """Load persistent state from Azure Blob Storage.

    Stores the blob ETag in state["_etag"] for optimistic concurrency on save.
    On first run (no blob exists), returns empty state with _etag = None.
    """
    try:
        blob = container.download_blob(BLOB_NAME)
        state = json.loads(blob.readall())
        state[ETAG_KEY] = blob.properties.etag
    except ResourceNotFoundError:
        logger.info("No existing state blob found — starting fresh")
        state = {ETAG_KEY: None}
    except Exception:
        # Auth errors, network errors, corrupt JSON — do NOT silently start fresh
        logger.exception("Failed to read state blob — aborting sync")
        raise

    # Ensure all expected keys exist
    for key, default in _DEFAULT_STATE.items():
        state.setdefault(key, default if not isinstance(default, (dict, list)) else type(default)())
    return state


def save_state(container: ContainerClient, state: dict) -> None:
    """Persist state to Azure Blob Storage with ETag-based optimistic concurrency.

    Raises StateConflictError if the blob was modified by another sync cycle
    since we last read it.
    """
    # Pop the ETag before serialising (don't persist it in the blob)
    etag = state.pop(ETAG_KEY, None)

    data = json.dumps(state, default=str)

    try:
        if etag is None:
            # First write — ensure we don't overwrite a blob created by a concurrent first-run
            container.upload_blob(BLOB_NAME, data, overwrite=False)
        else:
            # Subsequent writes — only succeed if the blob hasn't changed since we read it
            container.upload_blob(BLOB_NAME, data, overwrite=True, etag=etag, match_condition="IfMatch")
    except ResourceExistsError:
        # Another sync cycle created the blob between our read (not found) and write
        raise StateConflictError("State blob was created by another sync cycle during first run")
    except Exception as e:
        if hasattr(e, "status_code") and e.status_code == 412:
            raise StateConflictError("State blob was modified by another sync cycle") from e
        raise


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
