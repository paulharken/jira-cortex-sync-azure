"""Atlassian Document Format (ADF) builder helpers for Jira ticket descriptions."""

from datetime import datetime, timezone
from typing import Optional

from .config import Config


def _adf_text(text: str, bold: bool = False) -> dict:
    node: dict = {"type": "text", "text": text}
    if bold:
        node["marks"] = [{"type": "strong"}]
    return node


def _adf_paragraph(*inlines: dict) -> dict:
    return {"type": "paragraph", "content": list(inlines)}


def _adf_heading(text: str, level: int = 3) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [_adf_text(text)],
    }


def _adf_link(text: str, href: str) -> dict:
    return {
        "type": "text",
        "text": text,
        "marks": [{"type": "link", "attrs": {"href": href}}],
    }


def _adf_rule() -> dict:
    return {"type": "rule"}


def _adf_table_row(*cells: dict) -> dict:
    return {"type": "tableRow", "content": list(cells)}


def _adf_table_header(text: str) -> dict:
    return {
        "type": "tableHeader",
        "content": [_adf_paragraph(_adf_text(text, bold=True))],
    }


def _adf_table_cell(text: str) -> dict:
    return {
        "type": "tableCell",
        "content": [_adf_paragraph(_adf_text(text))],
    }


def _adf_table_cell_link(text: str, href: str) -> dict:
    return {
        "type": "tableCell",
        "content": [_adf_paragraph(_adf_link(text, href))],
    }


def _format_creation_time(raw: object) -> str:
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        except (OSError, ValueError):
            return str(raw)
    return str(raw) if raw else "N/A"


def build_case_description_adf(case: dict, config: Config) -> dict:
    """Build a rich ADF document for the Jira ticket description (case)."""
    case_id = case.get("case_id", "?")
    content: list[dict] = []

    # Cortex deep link
    console_url = config.cortex_console_url
    if console_url:
        case_url = f"{console_url}/case-management/case/{case_id}"
        content.append(
            _adf_paragraph(
                _adf_text("Open in Cortex: ", bold=True),
                _adf_link(case_url, case_url),
            )
        )
        content.append(_adf_rule())

    # Case details table
    created = _format_creation_time(case.get("creation_time"))
    severity = case.get("severity", "N/A")
    status = case.get("status_progress", "N/A")
    domain = case.get("case_domain", "N/A")
    owner = case.get("owner", "N/A")
    assigned = case.get("assigned_user_pretty_name", case.get("assigned_user_mail", "Unassigned"))
    score = case.get("aggregated_score")

    content.append(_adf_heading("Case Details"))
    header_row = _adf_table_row(
        _adf_table_header("Field"),
        _adf_table_header("Value"),
    )
    rows = [
        header_row,
        _adf_table_row(_adf_table_cell("Case ID"), _adf_table_cell(str(case_id))),
        _adf_table_row(_adf_table_cell("Domain"), _adf_table_cell(str(domain))),
        _adf_table_row(_adf_table_cell("Severity"), _adf_table_cell(str(severity))),
        _adf_table_row(_adf_table_cell("Status"), _adf_table_cell(str(status))),
        _adf_table_row(_adf_table_cell("Created"), _adf_table_cell(created)),
        _adf_table_row(_adf_table_cell("Owner"), _adf_table_cell(str(owner))),
        _adf_table_row(_adf_table_cell("Assigned To"), _adf_table_cell(str(assigned))),
    ]
    if score is not None:
        rows.append(_adf_table_row(_adf_table_cell("Score"), _adf_table_cell(str(score))))
    content.append({"type": "table", "content": rows})

    # Description
    desc = case.get("description", "No description")
    if desc:
        content.append(_adf_heading("Description"))
        content.append(_adf_paragraph(_adf_text(desc)))

    # Assets
    assets = case.get("assets", [])
    if assets:
        content.append(_adf_heading("Affected Assets"))
        asset_items = []
        for asset in assets:
            if isinstance(asset, dict):
                name = asset.get("name", asset.get("host_name", str(asset)))
                asset_type = asset.get("type", "")
                label = f"{name} ({asset_type})" if asset_type else name
            else:
                label = str(asset)
            asset_items.append({
                "type": "listItem",
                "content": [_adf_paragraph(_adf_text(label))],
            })
        content.append({"type": "bulletList", "content": asset_items})

    # Linked issues
    issues = case.get("issue_ids", case.get("issues", []))
    if issues:
        content.append(_adf_heading("Linked Cortex Issues"))
        if console_url:
            inlines: list[dict] = []
            for idx, issue_id in enumerate(issues):
                if idx > 0:
                    inlines.append(_adf_text(", "))
                issue_url = f"{console_url}/alerts-and-incidents/alerts/{issue_id}"
                inlines.append(_adf_link(str(issue_id), issue_url))
            content.append(_adf_paragraph(*inlines))
        else:
            content.append(
                _adf_paragraph(_adf_text(", ".join(str(i) for i in issues)))
            )

    return {"version": 1, "type": "doc", "content": content}


def build_issue_description_adf(issue: dict, config: Config) -> dict:
    """Build a rich ADF document for the Jira ticket description (standalone issue)."""
    issue_id = issue.get("id", "?")
    content: list[dict] = []

    # Cortex deep link
    console_url = config.cortex_console_url
    if console_url:
        issue_url = f"{console_url}/alerts-and-incidents/alerts/{issue_id}"
        content.append(
            _adf_paragraph(
                _adf_text("Open in Cortex: ", bold=True),
                _adf_link(issue_url, issue_url),
            )
        )
        content.append(_adf_rule())

    # Issue details table
    created = _format_creation_time(issue.get("observation_time"))
    severity = issue.get("severity", "N/A")
    status = issue.get("status", {})
    status_progress = status.get("progress", "N/A") if isinstance(status, dict) else str(status)
    domain = issue.get("issue_domain", "N/A")
    assigned = issue.get("assigned_to_pretty", issue.get("assigned_to", "Unassigned"))
    detection = issue.get("detection", {})
    detection_method = detection.get("method", "N/A") if isinstance(detection, dict) else "N/A"

    content.append(_adf_heading("Issue Details"))
    header_row = _adf_table_row(
        _adf_table_header("Field"),
        _adf_table_header("Value"),
    )
    rows = [
        header_row,
        _adf_table_row(_adf_table_cell("Issue ID"), _adf_table_cell(str(issue_id))),
        _adf_table_row(_adf_table_cell("Domain"), _adf_table_cell(str(domain))),
        _adf_table_row(_adf_table_cell("Severity"), _adf_table_cell(str(severity))),
        _adf_table_row(_adf_table_cell("Status"), _adf_table_cell(str(status_progress))),
        _adf_table_row(_adf_table_cell("Observed"), _adf_table_cell(created)),
        _adf_table_row(_adf_table_cell("Assigned To"), _adf_table_cell(str(assigned))),
        _adf_table_row(_adf_table_cell("Detection"), _adf_table_cell(str(detection_method))),
    ]
    content.append({"type": "table", "content": rows})

    # Description / name
    name = issue.get("name", issue.get("description", "No description"))
    if name:
        content.append(_adf_heading("Description"))
        content.append(_adf_paragraph(_adf_text(str(name))))

    # Assets
    assets = issue.get("assets", [])
    if assets:
        content.append(_adf_heading("Affected Assets"))
        asset_items = []
        for asset in assets:
            if isinstance(asset, dict):
                asset_name = asset.get("name", asset.get("host_name", str(asset)))
                asset_type = asset.get("type", "")
                label = f"{asset_name} ({asset_type})" if asset_type else asset_name
            else:
                label = str(asset)
            asset_items.append({
                "type": "listItem",
                "content": [_adf_paragraph(_adf_text(label))],
            })
        content.append({"type": "bulletList", "content": asset_items})

    return {"version": 1, "type": "doc", "content": content}
