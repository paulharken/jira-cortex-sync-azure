"""Sync engine: all orchestration logic for Cortex <-> Jira bidirectional sync."""

import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from azure.storage.blob import ContainerClient

from .adf_builder import build_case_description_adf, build_issue_description_adf
from .config import Config, MAX_RETRY_ATTEMPTS
from .cortex_client import CortexClient
from .jira_client import JiraClient
from .log import get_logger
from .state import get_state, save_state, prune_closed_records

logger = get_logger()


# ---------------------------------------------------------------------------
# Analyst assignment helper
# ---------------------------------------------------------------------------

def resolve_and_assign(
    jira: JiraClient, state: dict, issue_key: str, email: str,
) -> None:
    """Resolve a Cortex analyst email to a Jira account and assign the ticket."""
    if not email:
        return

    account_id = state["user_cache"].get(email)

    if not account_id:
        try:
            account_id = jira.search_user(email)
            if account_id:
                state["user_cache"][email] = account_id
                logger.info(f"Cached Jira user: {email} -> {account_id}")
            else:
                logger.info(f"No Jira user found for {email}")
                return
        except Exception:
            logger.error(f"Failed to look up Jira user for {email}: {traceback.format_exc()}")
            return

    try:
        jira.assign_issue(issue_key, account_id)
    except Exception:
        logger.error(f"Failed to assign {issue_key} to {account_id} ({email}): {traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Cortex -> Jira sync
# ---------------------------------------------------------------------------

def sync_cortex_to_jira(
    cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> dict:
    """Fetch new/changed Cortex cases and create Jira tickets. Returns stats."""
    stats = {"created": 0, "existing": 0, "failed": 0, "retried": 0, "pending_playbook": 0}

    # Process retry queue first
    stats["retried"] = _process_retry_queue(cortex, jira, state, config)

    # Build filters
    last_poll_ms = state["last_poll_ms"]
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    if last_poll_ms == 0:
        logger.info("Cortex->Jira: FIRST RUN -- fetching all non-resolved cases")
        filters = [
            {"field": "status_progress", "operator": "nin", "value": ["Resolved"]},
        ]
        if config.sync_from_date:
            from_ms = int(datetime.fromisoformat(config.sync_from_date).timestamp() * 1000)
            filters.append({"field": "last_update_time", "operator": "gte", "value": from_ms})
            logger.info(f"Cortex->Jira: applying sync_from_date floor {config.sync_from_date}")
    else:
        since_ms = last_poll_ms - 60000  # 60s lookback buffer
        filters = [
            {"field": "last_update_time", "operator": "gte", "value": since_ms},
            {"field": "status_progress", "operator": "nin", "value": ["Resolved"]},
        ]
        logger.info(f"Cortex->Jira: fetching cases updated since {since_ms}")

    cases = cortex.search_cases(filters=filters)
    domain = config.cortex_case_domain.lower()
    max_cases = config.max_sync_cases

    for case in cases:
        case_id = str(case.get("case_id", ""))
        case_domain = case.get("case_domain", "")
        case_status = case.get("status_progress", "")

        if case_domain.lower() != domain:
            continue
        if case_status.lower() == "resolved":
            continue
        if max_cases > 0 and stats["created"] >= max_cases:
            break

        # Skip cases whose playbooks haven't finished yet
        issue_ids = [str(i) for i in case.get("issue_ids", case.get("issues", []))]
        if not cortex.case_playbooks_ready(issue_ids):
            logger.info(f"Case {case_id}: playbooks not yet completed -- deferring to next cycle")
            stats["pending_playbook"] += 1
            continue

        result = _handle_case(case, cortex, jira, state, config)
        if result == "created":
            stats["created"] += 1
        elif result == "existing":
            stats["existing"] += 1
        else:
            stats["failed"] += 1
            _enqueue_retry(state, case_id, case)

    # Update poll timestamp
    state["last_poll_ms"] = now_ms

    logger.info(
        f"Cortex->Jira: {stats['created']} created, {stats['existing']} existing, "
        f"{stats['failed']} failed, {stats['retried']} retried, "
        f"{stats['pending_playbook']} awaiting playbooks"
    )
    return stats


def _handle_case(
    case: dict, cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> str:
    """Handle a single case. Returns 'created', 'existing', or 'failed'."""
    case_id = str(case["case_id"])
    description = case.get("description", "No description")
    severity = case.get("severity", "MEDIUM")
    issue_ids = [str(i) for i in case.get("issue_ids", case.get("issues", []))]

    # Already synced and open?
    existing = state["sync_records"].get(case_id)
    if existing and existing["status"] == "open":
        _sync_severity_change(existing, severity, jira)
        _sync_new_issues(existing, issue_ids, cortex, jira, state, config)
        return "existing"

    # Previously closed? (reopen scenario)
    closed_jira_key = None
    if existing and existing["status"] == "closed":
        closed_jira_key = existing["jira_key"]

    # Build ticket fields
    summary = f"[CORTEX-{case_id}] {description.replace(chr(10), ' ').replace(chr(13), ' ').strip()}"
    description_adf = build_case_description_adf(case, config)
    extra_fields: dict = {}
    if config.jira_case_id_field:
        extra_fields[config.jira_case_id_field] = str(case_id)
    if config.jira_xdr_url_field and config.cortex_console_url:
        xdr_url = f"{config.cortex_console_url}/case-management/case/{case_id}"
        extra_fields[config.jira_xdr_url_field] = xdr_url

    # Duplicate detection
    if config.jira_case_id_field:
        try:
            existing_key = jira.find_ticket_by_field(config.jira_case_id_field, str(case_id))
            if existing_key:
                logger.info(f"Duplicate detected: case {case_id} already has {existing_key}")
                state["sync_records"][case_id] = {
                    "jira_key": existing_key,
                    "severity": severity.upper(),
                    "issue_ids": issue_ids,
                    "status": "open",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                return "created"
        except Exception:
            logger.info(f"Duplicate check failed for case {case_id} -- proceeding with creation")

    # Create Jira ticket
    try:
        jira_key = jira.create_issue(summary, description_adf, severity, extra_fields or None)
    except Exception:
        logger.error(f"Failed to create Jira issue for case {case_id}: {traceback.format_exc()}")
        return "failed"

    # Link to previous ticket if reopened
    if closed_jira_key:
        try:
            jira.link_issues(jira_key, closed_jira_key)
            logger.info(f"Case {case_id} reopened: linked {jira_key} to {closed_jira_key}")
        except Exception:
            logger.error(f"Failed to link {jira_key} to {closed_jira_key}")

    # Auto-assign if Cortex case has an assigned analyst
    assigned_email = case.get("assigned_user_mail", "")
    if assigned_email:
        resolve_and_assign(jira, state, jira_key, assigned_email)

    # Record in state
    state["sync_records"][case_id] = {
        "jira_key": jira_key,
        "severity": severity.upper(),
        "issue_ids": issue_ids,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(f"Synced case {case_id} -> {jira_key}")
    return "created"


def _sync_severity_change(record: dict, current_severity: str, jira: JiraClient) -> None:
    stored = record.get("severity", "MEDIUM")
    if current_severity.upper() == stored.upper():
        return
    try:
        jira.update_priority(record["jira_key"], current_severity)
        record["severity"] = current_severity.upper()
        logger.info(f"Severity updated {record['jira_key']}: {stored} -> {current_severity}")
    except Exception:
        logger.error(f"Failed to update priority on {record['jira_key']}: {traceback.format_exc()}")


def _sync_new_issues(
    record: dict, current_issue_ids: list[str],
    cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> None:
    known_ids = set(record.get("issue_ids", []))
    current_ids = {str(i) for i in current_issue_ids}
    new_ids = current_ids - known_ids

    if not new_ids:
        return

    console_url = config.cortex_console_url
    for issue_id in new_ids:
        if console_url:
            issue_url = f"{console_url}/alerts-and-incidents/alerts/{issue_id}"
            comment = f"New issue added: #{issue_id} -- {issue_url}"
        else:
            comment = f"New issue added: #{issue_id}"
        try:
            jira.add_comment(record["jira_key"], comment)
        except Exception:
            logger.error(f"Failed to add comment to {record['jira_key']} for issue {issue_id}")

    record["issue_ids"] = list(known_ids | current_ids)


# ---------------------------------------------------------------------------
# Open case checker (bidirectional closure)
# ---------------------------------------------------------------------------

def check_open_cases(
    cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> dict:
    """Re-check all open synced cases for closures and severity changes."""
    stats = {"cortex_closed": 0, "jira_closed": 0, "severity_updated": 0}

    open_records = {
        cid: rec for cid, rec in state["sync_records"].items()
        if rec["status"] == "open"
    }
    if not open_records:
        return stats

    # Batch-fetch current case state from Cortex
    case_ids = [int(cid) for cid in open_records.keys()]
    cases = cortex.search_cases(
        filters=[{"field": "case_id", "operator": "in", "value": case_ids}]
    )
    case_map = {str(c["case_id"]): c for c in cases}

    for case_id, record in open_records.items():
        case = case_map.get(case_id)

        # Cortex case resolved?
        if case and case.get("status_progress", "").lower() == "resolved":
            record["status"] = "closed"
            record["closed_at"] = datetime.now(timezone.utc).isoformat()
            stats["cortex_closed"] += 1
            logger.info(f"Case {case_id} resolved in Cortex -- marked closed ({record['jira_key']})")
            continue

        # Jira ticket Done?
        try:
            jira_detail = jira.get_issue_detail(record["jira_key"])
            if jira_detail.get("status_category") == "done":
                _close_cortex_case(int(case_id), record["jira_key"], cortex, jira, config)
                record["status"] = "closed"
                record["closed_at"] = datetime.now(timezone.utc).isoformat()
                stats["jira_closed"] += 1
                logger.info(f"Jira {record['jira_key']} Done -- resolved case {case_id}")
                continue
        except Exception:
            logger.error(f"Failed to check Jira status for {record['jira_key']}: {traceback.format_exc()}")

        # Severity change?
        if case:
            current_severity = case.get("severity", "MEDIUM")
            if current_severity.upper() != record.get("severity", "MEDIUM").upper():
                _sync_severity_change(record, current_severity, jira)
                stats["severity_updated"] += 1

            # New issues on case?
            current_issue_ids = [str(i) for i in case.get("issue_ids", case.get("issues", []))]
            _sync_new_issues(record, current_issue_ids, cortex, jira, state, config)

    logger.info(
        f"Open case check: {stats['cortex_closed']} Cortex-closed, "
        f"{stats['jira_closed']} Jira-closed, {stats['severity_updated']} severity updates"
    )
    return stats


def _close_cortex_case(
    case_id: int, jira_key: str, cortex: CortexClient, jira: JiraClient, config: Config,
) -> None:
    """Resolve a Cortex case when its Jira ticket reaches Done."""
    try:
        resolution_map: dict = json.loads(config.resolution_type_map) if config.resolution_type_map else {}
    except (json.JSONDecodeError, TypeError):
        logger.info("Failed to parse resolution_type_map -- using default")
        resolution_map = {}

    default_reason = config.default_resolution_type or "Resolved - Other"
    resolve_reason = default_reason

    # Read Jira changelog to find pre-Done status
    try:
        transitions = jira.get_changelog(jira_key)
        if transitions:
            last_from_status = transitions[-1].get("from_status", "")
            if last_from_status:
                mapped = resolution_map.get(last_from_status)
                if mapped:
                    resolve_reason = mapped
                    logger.info(f"Closure: '{last_from_status}' -> '{resolve_reason}'")
                else:
                    logger.info(f"Closure: '{last_from_status}' not in map -- using default '{default_reason}'")
    except Exception:
        logger.error(f"Failed to fetch changelog for {jira_key}: {traceback.format_exc()}")

    # Resolve the Cortex case
    try:
        cortex.update_case(
            case_id=case_id,
            status="Resolved",
            reason=resolve_reason,
            comment=f"Resolved via Jira {jira_key}",
        )
    except Exception:
        logger.error(f"Failed to resolve Cortex case {case_id}: {traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Jira -> Cortex sync
# ---------------------------------------------------------------------------

def sync_jira_to_cortex(
    cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> dict:
    """Find Jira alerts closed since last poll and resolve matching Cortex cases."""
    stats = {"resolved": 0, "issue_closed": 0}

    last_poll_iso = state.get("last_jira_poll_iso", "")
    if not last_poll_iso:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
    else:
        since = datetime.fromisoformat(last_poll_iso) - timedelta(seconds=60)

    now = datetime.now(timezone.utc)
    since_str = since.strftime("%Y-%m-%d %H:%M")

    logger.info(f"Jira->Cortex: searching for alerts closed since {since_str}")
    try:
        closed_alerts = jira.search_closed_alerts(since_str)
    except Exception:
        logger.error(f"Failed to search Jira for closed alerts: {traceback.format_exc()}")
        return stats

    for alert in closed_alerts:
        jira_key = alert["key"]

        # Check case records
        for case_id, record in state["sync_records"].items():
            if record["jira_key"] == jira_key and record["status"] == "open":
                try:
                    _close_cortex_case(int(case_id), jira_key, cortex, jira, config)
                    record["status"] = "closed"
                    record["closed_at"] = datetime.now(timezone.utc).isoformat()
                    stats["resolved"] += 1
                except Exception:
                    logger.error(f"Failed to resolve case {case_id} for {jira_key}: {traceback.format_exc()}")
                break

        # Check issue records
        for issue_id, record in state["issue_sync_records"].items():
            if record["jira_key"] == jira_key and record["status"] == "open":
                record["status"] = "closed"
                record["closed_at"] = datetime.now(timezone.utc).isoformat()
                stats["issue_closed"] += 1
                break

    state["last_jira_poll_iso"] = now.isoformat()
    logger.info(f"Jira->Cortex: {stats['resolved']} cases resolved, {stats['issue_closed']} issues closed")
    return stats


# ---------------------------------------------------------------------------
# Standalone issue sync
# ---------------------------------------------------------------------------

def sync_issues_to_jira(
    cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> dict:
    """Sync assigned standalone Cortex issues to Jira tickets."""
    stats = {"created": 0, "skipped": 0}

    domain = config.cortex_case_domain.lower()
    filters = [
        {"field": "issue_domain", "operator": "in", "value": [domain.capitalize()]},
    ]
    if config.sync_from_date:
        from_ms = int(datetime.fromisoformat(config.sync_from_date).timestamp() * 1000)
        filters.append({"field": "observation_time", "operator": "gte", "value": from_ms})

    logger.info(f"Issue sync: searching {domain} domain issues")
    all_issues = cortex.search_issues_filtered(filters=filters)

    # Build exclusion sets
    case_issue_ids: set[str] = set()
    for record in state["sync_records"].values():
        case_issue_ids.update(record.get("issue_ids", []))

    synced_issue_ids = set(state["issue_sync_records"].keys())

    for issue in all_issues:
        issue_id = str(issue.get("id", ""))
        if not issue_id:
            continue

        if issue_id in case_issue_ids:
            continue

        if issue_id in synced_issue_ids:
            continue

        assigned_to = issue.get("assigned_to", "")
        if not assigned_to:
            stats["skipped"] += 1
            continue

        status = issue.get("status", {})
        status_progress = status.get("progress", "") if isinstance(status, dict) else str(status)
        if status_progress.upper() == "RESOLVED":
            continue

        pb_state = cortex.get_playbook_state(issue_id)
        if pb_state != "completed":
            logger.info(f"Issue {issue_id}: playbook not yet completed (state={pb_state}) -- deferring")
            continue

        # Build Jira ticket
        severity = issue.get("severity", "MEDIUM")
        name = issue.get("name", issue.get("description", "No description"))
        summary = f"[CORTEX-ISSUE-{issue_id}] {name}"
        description_adf = build_issue_description_adf(issue, config)

        extra_fields: dict = {}
        if config.jira_issue_id_field:
            extra_fields[config.jira_issue_id_field] = str(issue_id)
        if config.jira_xdr_url_field and config.cortex_console_url:
            xdr_url = f"{config.cortex_console_url}/alerts-and-incidents/alerts/{issue_id}"
            extra_fields[config.jira_xdr_url_field] = xdr_url

        # Duplicate detection
        if config.jira_issue_id_field:
            try:
                existing_key = jira.find_ticket_by_field(config.jira_issue_id_field, str(issue_id))
                if existing_key:
                    logger.info(f"Issue {issue_id} already has Jira ticket {existing_key}")
                    state["issue_sync_records"][issue_id] = {
                        "jira_key": existing_key,
                        "status": "open",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    stats["created"] += 1
                    continue
            except Exception:
                logger.info(f"Duplicate check failed for issue {issue_id} -- proceeding")

        # Create ticket
        try:
            jira_key = jira.create_issue(summary, description_adf, severity, extra_fields or None)
        except Exception:
            logger.error(f"Failed to create Jira issue for Cortex issue {issue_id}: {traceback.format_exc()}")
            continue

        resolve_and_assign(jira, state, jira_key, assigned_to)

        state["issue_sync_records"][issue_id] = {
            "jira_key": jira_key,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        stats["created"] += 1
        logger.info(f"Synced issue {issue_id} -> {jira_key} (assigned: {assigned_to})")

    logger.info(f"Issue sync: {stats['created']} created, {stats['skipped']} skipped (unassigned)")
    return stats


# ---------------------------------------------------------------------------
# Retry queue
# ---------------------------------------------------------------------------

def _enqueue_retry(state: dict, case_id: str, case: dict) -> None:
    """Add a failed case to the retry queue with exponential backoff."""
    for entry in state["retry_queue"]:
        if str(entry["case_id"]) == str(case_id):
            entry["attempts"] += 1
            backoff_ms = min(60 * 60 * 1000, (2 ** entry["attempts"]) * 60 * 1000)
            entry["next_retry_ms"] = int(time.time() * 1000) + backoff_ms
            return

    state["retry_queue"].append({
        "case_id": str(case_id),
        "case_json": json.dumps(case, default=str),
        "attempts": 1,
        "next_retry_ms": int(time.time() * 1000) + 120000,
    })


def _process_retry_queue(
    cortex: CortexClient, jira: JiraClient, state: dict, config: Config,
) -> int:
    """Process due retry entries. Returns count of successful retries."""
    if not state["retry_queue"]:
        return 0

    now_ms = int(time.time() * 1000)
    succeeded = 0
    remaining: list[dict] = []

    for entry in state["retry_queue"]:
        case_id = str(entry["case_id"])
        attempts = entry["attempts"]

        if entry["next_retry_ms"] > now_ms:
            remaining.append(entry)
            continue

        if attempts >= MAX_RETRY_ATTEMPTS:
            logger.error(f"Retry queue: case {case_id} abandoned after {attempts} attempts")
            continue

        try:
            case = json.loads(entry["case_json"])
        except json.JSONDecodeError:
            logger.error(f"Retry queue: invalid JSON for case {case_id}, dropping")
            continue

        logger.info(f"Retry queue: retrying case {case_id} (attempt {attempts + 1}/{MAX_RETRY_ATTEMPTS})")
        result = _handle_case(case, cortex, jira, state, config)

        if result in ("created", "existing"):
            succeeded += 1
            logger.info(f"Retry queue: case {case_id} succeeded")
        else:
            entry["attempts"] += 1
            backoff_ms = min(60 * 60 * 1000, (2 ** entry["attempts"]) * 60 * 1000)
            entry["next_retry_ms"] = now_ms + backoff_ms
            remaining.append(entry)

    state["retry_queue"] = remaining
    if succeeded:
        logger.info(f"Retry queue: {succeeded} succeeded, {len(remaining)} remaining")
    return succeeded


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def run_sync(config: Config, container: ContainerClient) -> dict:
    """Execute the full sync cycle. Returns a results dict."""
    errors = config.validate()
    if errors:
        logger.error(f"Configuration errors: {'; '.join(errors)}")
        return {"error": f"Configuration errors: {'; '.join(errors)}"}

    cortex = CortexClient(config)
    jira = JiraClient(config)
    state = get_state(container)

    results: dict[str, Any] = {}

    # Phase 1: Cortex -> Jira (new cases)
    try:
        results["cortex_to_jira"] = sync_cortex_to_jira(cortex, jira, state, config)
    except Exception:
        logger.error(f"Cortex->Jira sync failed: {traceback.format_exc()}")
        results["cortex_to_jira"] = {"error": traceback.format_exc()}

    # Phase 2: Check open cases (bidirectional closure, severity sync)
    try:
        results["open_case_check"] = check_open_cases(cortex, jira, state, config)
    except Exception:
        logger.error(f"Open case check failed: {traceback.format_exc()}")
        results["open_case_check"] = {"error": traceback.format_exc()}

    # Phase 3: Jira -> Cortex (closed alerts)
    try:
        results["jira_to_cortex"] = sync_jira_to_cortex(cortex, jira, state, config)
    except Exception:
        logger.error(f"Jira->Cortex sync failed: {traceback.format_exc()}")
        results["jira_to_cortex"] = {"error": traceback.format_exc()}

    # Phase 4: Standalone issue sync (if enabled)
    if config.sync_issues:
        try:
            results["issue_sync"] = sync_issues_to_jira(cortex, jira, state, config)
        except Exception:
            logger.error(f"Issue sync failed: {traceback.format_exc()}")
            results["issue_sync"] = {"error": traceback.format_exc()}
    else:
        results["issue_sync"] = {"skipped": True}

    # Housekeeping
    pruned = prune_closed_records(state)
    if pruned:
        logger.info(f"Pruned {pruned} closed records older than 7 days")

    # Persist state
    save_state(container, state)

    # Summary
    open_cases = sum(1 for r in state["sync_records"].values() if r["status"] == "open")
    open_issues = sum(1 for r in state["issue_sync_records"].values() if r["status"] == "open")
    retry_count = len(state["retry_queue"])

    summary = (
        f"Sync complete. "
        f"Cases: {open_cases} open, {len(state['sync_records'])} total. "
        f"Issues: {open_issues} open, {len(state['issue_sync_records'])} total. "
        f"Retry queue: {retry_count}."
    )
    logger.info(summary)
    results["summary"] = summary
    return results


def test_connectivity(config: Config) -> dict:
    """Verify connectivity to both Cortex and Jira. Returns status dict."""
    errors = config.validate()
    if errors:
        return {"status": "error", "message": f"Configuration errors: {'; '.join(errors)}"}

    result = {"cortex": "unknown", "jira": "unknown"}

    # Test Cortex
    try:
        cortex = CortexClient(config)
        cases = cortex.search_cases(
            filters=[{"field": "status_progress", "operator": "nin", "value": ["Resolved"]}]
        )
        result["cortex"] = f"ok ({len(cases)} non-resolved cases)"
    except Exception as e:
        result["cortex"] = f"failed: {e}"

    # Test Jira
    try:
        jira = JiraClient(config)
        url = f"{jira.base_url}/rest/api/3/myself"
        resp = jira._request("GET", url)
        resp.raise_for_status()
        user = resp.json()
        result["jira"] = f"ok (authenticated as {user.get('displayName', 'unknown')})"
    except Exception as e:
        result["jira"] = f"failed: {e}"

    result["status"] = "ok" if "ok" in result["cortex"] and "ok" in result["jira"] else "error"
    return result
