"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass


SEVERITY_TO_PRIORITY = {
    "INFORMATIONAL": "Lowest",
    "LOW": "Low",
    "MEDIUM": "Medium",
    "HIGH": "High",
    "CRITICAL": "Highest",
    # Cortex cases return lowercase severity
    "informational": "Lowest",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "critical": "Highest",
}

MAX_RETRY_ATTEMPTS = 5
CLOSED_RECORD_TTL_DAYS = 7
PAGE_SIZE = 100


@dataclass
class Config:
    cortex_base_url: str
    cortex_api_key: str
    cortex_api_key_id: str
    cortex_console_url: str
    cortex_case_domain: str
    jira_site_url: str
    jira_cloud_id: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str
    jira_issue_type: str
    jira_case_id_field: str
    jira_issue_id_field: str
    jira_xdr_url_field: str
    resolution_type_map: str
    default_resolution_type: str
    max_sync_cases: int
    sync_issues: bool
    sync_from_date: str

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            cortex_base_url=os.environ.get("CORTEX_BASE_URL", "").rstrip("/"),
            cortex_api_key=os.environ.get("CORTEX_API_KEY", ""),
            cortex_api_key_id=os.environ.get("CORTEX_API_KEY_ID", ""),
            cortex_console_url=os.environ.get("CORTEX_CONSOLE_URL", "").rstrip("/"),
            cortex_case_domain=os.environ.get("CORTEX_CASE_DOMAIN", "security"),
            jira_site_url=os.environ.get("JIRA_SITE_URL", "").rstrip("/"),
            jira_cloud_id=os.environ.get("JIRA_CLOUD_ID", ""),
            jira_email=os.environ.get("JIRA_EMAIL", ""),
            jira_api_token=os.environ.get("JIRA_API_TOKEN", ""),
            jira_project_key=os.environ.get("JIRA_PROJECT_KEY", ""),
            jira_issue_type=os.environ.get("JIRA_ISSUE_TYPE", "Alert"),
            jira_case_id_field=os.environ.get("JIRA_CASE_ID_FIELD", ""),
            jira_issue_id_field=os.environ.get("JIRA_ISSUE_ID_FIELD", ""),
            jira_xdr_url_field=os.environ.get("JIRA_XDR_URL_FIELD", ""),
            resolution_type_map=os.environ.get(
                "RESOLUTION_TYPE_MAP",
                '{"False Positive": "Resolved - False Positive", '
                '"Duplicate": "Resolved - Duplicate Case", '
                '"Known Issue": "Resolved - Known Issue", '
                '"Security Testing": "Resolved - Security Testing", '
                '"TP Malicious": "Resolved - TP Malicious", '
                '"TP Benign": "Resolved - TP Benign", '
                '"SPAM": "Resolved - SPAM or Marketing"}',
            ),
            default_resolution_type=os.environ.get("DEFAULT_RESOLUTION_TYPE", "Resolved - Other"),
            max_sync_cases=int(os.environ.get("MAX_SYNC_CASES", "0")),
            sync_issues=os.environ.get("SYNC_ISSUES", "false").lower() in ("true", "1", "yes"),
            sync_from_date=os.environ.get("SYNC_FROM_DATE", ""),
        )

    def validate(self) -> list[str]:
        """Validate required fields. Returns list of error messages (empty = valid)."""
        errors = []
        required = {
            "CORTEX_BASE_URL": self.cortex_base_url,
            "CORTEX_API_KEY": self.cortex_api_key,
            "CORTEX_API_KEY_ID": self.cortex_api_key_id,
            "JIRA_EMAIL": self.jira_email,
            "JIRA_API_TOKEN": self.jira_api_token,
            "JIRA_PROJECT_KEY": self.jira_project_key,
        }
        for name, value in required.items():
            if not value or not value.strip():
                errors.append(f"Missing required setting: {name}")
        if not self.jira_cloud_id.strip() and not self.jira_site_url.strip():
            errors.append("Either JIRA_CLOUD_ID or JIRA_SITE_URL must be set")
        return errors
