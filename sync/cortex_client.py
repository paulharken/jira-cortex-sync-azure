"""Cortex XSIAM API client."""

import time
import traceback
from typing import Optional

import requests

from .config import Config, PAGE_SIZE
from .log import get_logger

logger = get_logger()


class CortexClient:
    def __init__(self, config: Config):
        self.base_url = config.cortex_base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": config.cortex_api_key,
            "x-xdr-auth-id": config.cortex_api_key_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP request with retry on 429/503."""
        delay = 1.0
        for attempt in range(4):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code not in (429, 503):
                return resp
            logger.info(f"Cortex HTTP {resp.status_code} on attempt {attempt + 1}/4, retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
        return resp

    def search_cases(self, filters: Optional[list[dict]] = None) -> list[dict]:
        all_cases: list[dict] = []
        search_from = 0
        url = f"{self.base_url}/public_api/v1/case/search"

        while True:
            body = {
                "request_data": {
                    "filters": filters or [],
                    "search_from": search_from,
                    "search_to": search_from + PAGE_SIZE,
                    "sort": {"field": "creation_time", "keyword": "asc"},
                }
            }
            resp = self._request("POST", url, json=body)
            resp.raise_for_status()
            data = resp.json()
            cases = data.get("reply", {}).get("DATA", [])
            all_cases.extend(cases)
            total = data.get("reply", {}).get("TOTAL_COUNT", 0)
            filter_count = data.get("reply", {}).get("FILTER_COUNT", total)
            effective_total = min(total, filter_count) if filter_count else total
            search_from += PAGE_SIZE
            if not cases or search_from >= effective_total:
                break

        logger.info(f"Cortex: fetched {len(all_cases)} cases")
        return all_cases

    def update_case(self, case_id: int, status: str, reason: str, comment: str = "") -> None:
        body = {
            "request_data": {
                "update_data": {
                    "status_progress": status,
                    "resolve_reason": reason,
                    "resolve_comment": comment,
                }
            }
        }
        url = f"{self.base_url}/public_api/v1/case/update/{case_id}"
        resp = self._request("POST", url, json=body)
        resp.raise_for_status()
        # 204 No Content on success -- do NOT call .json()
        logger.info(f"Cortex case {case_id} updated: status={status} reason={reason}")

    def get_playbook_state(self, issue_id) -> Optional[str]:
        """Get playbook execution state for an issue/investigation.

        Returns the playbook state string (e.g. 'completed', 'inprogress', 'error')
        or None if the playbook data couldn't be retrieved.
        """
        url = f"{self.base_url}/xsoar/inv-playbook/{issue_id}"
        try:
            resp = self._request("GET", url)
            if not resp.ok:
                logger.debug(f"Playbook check for issue {issue_id}: HTTP {resp.status_code}")
                return None
            return resp.json().get("state")
        except Exception:
            logger.debug(f"Playbook check failed for issue {issue_id}: {traceback.format_exc()}")
            return None

    def case_playbooks_ready(self, issue_ids: list) -> bool:
        """Check if all playbooks for a case's issues have completed.

        Returns True if every issue's playbook state is 'completed',
        False if any are still running or couldn't be checked.
        """
        if not issue_ids:
            return True
        for issue_id in issue_ids:
            state = self.get_playbook_state(issue_id)
            if state != "completed":
                logger.debug(f"Issue {issue_id} playbook not ready: state={state}")
                return False
        return True

    def search_issues_filtered(self, filters: Optional[list[dict]] = None) -> list[dict]:
        all_issues: list[dict] = []
        search_from = 0
        url = f"{self.base_url}/public_api/v1/issue/search"

        while True:
            body = {
                "request_data": {
                    "filters": filters or [],
                    "search_from": search_from,
                    "search_to": search_from + PAGE_SIZE,
                }
            }
            resp = self._request("POST", url, json=body)
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("reply", {}).get("DATA", [])
            all_issues.extend(issues)
            total = data.get("reply", {}).get("TOTAL_COUNT", 0)
            filter_count = data.get("reply", {}).get("FILTER_COUNT", total)
            effective_total = min(total, filter_count) if filter_count else total
            search_from += PAGE_SIZE
            if not issues or search_from >= effective_total:
                break

        logger.info(f"Cortex: fetched {len(all_issues)} issues")
        return all_issues
