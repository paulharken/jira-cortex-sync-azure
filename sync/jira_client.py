"""Jira Cloud API client."""

import base64
import time
from typing import Optional

import requests

from .config import Config, SEVERITY_TO_PRIORITY
from .log import get_logger

logger = get_logger()


class JiraClient:
    def __init__(self, config: Config):
        if config.jira_cloud_id:
            self.base_url = f"https://api.atlassian.com/ex/jira/{config.jira_cloud_id}"
        else:
            self.base_url = config.jira_site_url
            logger.info(f"No Jira Cloud ID -- using site URL: {self.base_url}")
        self.project_key = config.jira_project_key
        self.issue_type = config.jira_issue_type
        token = base64.b64encode(
            f"{config.jira_email}:{config.jira_api_token}".encode()
        ).decode()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.session.timeout = 30

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP request with retry on 429/503."""
        delay = 1.0
        for attempt in range(4):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code not in (429, 503):
                return resp
            logger.info(f"Jira HTTP {resp.status_code} on attempt {attempt + 1}/4, retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
        return resp

    def create_issue(
        self, summary: str, description_adf: dict, severity: str,
        extra_fields: Optional[dict] = None,
    ) -> str:
        priority = SEVERITY_TO_PRIORITY.get(severity, "Medium")
        body = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary[:255],
                "description": description_adf,
                "issuetype": {"name": self.issue_type},
                "priority": {"name": priority},
            }
        }
        if extra_fields:
            body["fields"].update(extra_fields)
        url = f"{self.base_url}/rest/api/3/issue"
        logger.info(f"Creating Jira issue: project={self.project_key} priority={priority}")
        resp = self._request("POST", url, json=body)
        resp.raise_for_status()
        key = resp.json()["key"]
        logger.info(f"Created Jira issue {key}")
        return key

    def update_priority(self, issue_key: str, severity: str) -> None:
        priority = SEVERITY_TO_PRIORITY.get(severity, "Medium")
        body = {"fields": {"priority": {"name": priority}}}
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        resp = self._request("PUT", url, json=body)
        resp.raise_for_status()
        logger.info(f"Updated {issue_key} priority to {priority}")

    def add_comment(self, issue_key: str, body_text: str) -> None:
        body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": body_text}]}
                ],
            }
        }
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
        resp = self._request("POST", url, json=body)
        resp.raise_for_status()
        logger.debug(f"Added comment to {issue_key}")

    def search_closed_alerts(self, updated_since: str) -> list[dict]:
        jql = (
            f'project = "{self.project_key}" '
            f'AND issuetype = "{self.issue_type}" '
            f'AND status changed to "Closed" AFTER "{updated_since}"'
        )
        url = f"{self.base_url}/rest/api/3/search/jql"
        body = {"jql": jql, "fields": ["key", "status", "updated"]}
        resp = self._request("POST", url, json=body)
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        logger.info(f"Found {len(issues)} closed Jira alerts since {updated_since}")
        return issues

    def get_issue_detail(self, issue_key: str) -> dict:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}?fields=summary,status,created"
        resp = self._request("GET", url)
        resp.raise_for_status()
        data = resp.json()
        fields = data.get("fields", {})
        status_obj = fields.get("status") or {}
        return {
            "summary": fields.get("summary", ""),
            "status": status_obj.get("name", ""),
            "status_category": (status_obj.get("statusCategory") or {}).get("key", ""),
            "created": fields.get("created", ""),
        }

    def get_changelog(self, issue_key: str) -> list[dict]:
        transitions: list[dict] = []
        start_at = 0
        while True:
            url = (
                f"{self.base_url}/rest/api/3/issue/{issue_key}/changelog"
                f"?startAt={start_at}&maxResults=100"
            )
            resp = self._request("GET", url)
            resp.raise_for_status()
            data = resp.json()
            for history in data.get("values", []):
                author = history.get("author", {})
                created = history.get("created", "")
                for item in history.get("items", []):
                    if item.get("field") == "status":
                        transitions.append({
                            "author_id": author.get("accountId", ""),
                            "author_name": author.get("displayName", "Unknown"),
                            "created": created,
                            "from_status": item.get("fromString", ""),
                            "to_status": item.get("toString", ""),
                        })
            total = data.get("total", 0)
            start_at += len(data.get("values", []))
            if start_at >= total:
                break
        return transitions

    def find_ticket_by_field(self, field_id: str, value: str) -> Optional[str]:
        # JQL requires cf[NNNNN] syntax for custom fields, not "customfield_NNNNN"
        if field_id.startswith("customfield_"):
            cf_num = field_id.replace("customfield_", "")
            jql_field = f"cf[{cf_num}]"
        else:
            jql_field = f'"{field_id}"'
        jql = f'project = "{self.project_key}" AND {jql_field} = "{value}"'
        url = f"{self.base_url}/rest/api/3/search/jql"
        body = {"jql": jql, "fields": ["key"], "maxResults": 1}
        resp = self._request("POST", url, json=body)
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        if issues:
            key = issues[0]["key"]
            logger.info(f"Duplicate check: found {key} for {field_id}={value}")
            return key
        return None

    def link_issues(self, inward_key: str, outward_key: str) -> None:
        body = {
            "type": {"name": "Relates"},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        }
        url = f"{self.base_url}/rest/api/3/issueLink"
        resp = self._request("POST", url, json=body)
        resp.raise_for_status()
        logger.debug(f"Linked {inward_key} -> {outward_key}")

    def search_user(self, email: str) -> Optional[str]:
        """Search for a Jira user by email. Returns accountId or None."""
        url = f"{self.base_url}/rest/api/3/user/search?query={email}&maxResults=10"
        resp = self._request("GET", url)
        resp.raise_for_status()
        for user in resp.json():
            if user.get("accountType") == "atlassian" and user.get("active", False):
                return user.get("accountId", "")
        return None

    def assign_issue(self, issue_key: str, account_id: str) -> None:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/assignee"
        resp = self._request("PUT", url, json={"accountId": account_id})
        resp.raise_for_status()
        logger.debug(f"Assigned {issue_key} to {account_id}")

    def get_project_statuses(self) -> list[str]:
        """Get all workflow status names for the configured project and issue type."""
        url = f"{self.base_url}/rest/api/3/project/{self.project_key}/statuses"
        resp = self._request("GET", url)
        resp.raise_for_status()
        statuses: set[str] = set()
        for issuetype in resp.json():
            if issuetype.get("name", "").lower() == self.issue_type.lower():
                for status in issuetype.get("statuses", []):
                    statuses.add(status["name"])
        # If no match on issue type, return all statuses across all types
        if not statuses:
            for issuetype in resp.json():
                for status in issuetype.get("statuses", []):
                    statuses.add(status["name"])
        return sorted(statuses)
