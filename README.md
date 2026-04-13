# Jira Cortex Sync - Azure Speed Edition

Bidirectional sync engine between **Palo Alto Cortex XSIAM** and **Atlassian Jira Cloud**, deployed as a serverless **Azure Function App**. Runs a 60-second polling loop that keeps Cortex cases and Jira tickets in lockstep — new cases create tickets, severity changes propagate, and closures on either side resolve the other.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Deployment](#deployment)
- [HTTP Endpoints](#http-endpoints)
- [Configuration Reference](#configuration-reference)
- [Local Development](#local-development)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Case-to-ticket sync** — New Cortex XSIAM cases automatically create Jira tickets with rich ADF descriptions (deep links, case details table, affected assets, linked issues)
- **Bidirectional closure** — Resolving a Cortex case marks the Jira ticket; closing a Jira ticket resolves the Cortex case with a mapped resolution reason
- **Severity sync** — Priority changes in Cortex propagate to Jira ticket priority in real time
- **Standalone issue sync** — Optionally syncs assigned Cortex issues that aren't linked to any case
- **Analyst auto-assignment** — Maps Cortex analyst emails to Jira accounts and assigns tickets automatically (with caching)
- **Playbook-aware** — Defers ticket creation until all Cortex playbooks on a case have completed
- **Retry queue** — Failed ticket creations are retried with exponential backoff (up to 5 attempts)
- **Duplicate detection** — Looks up existing Jira tickets by custom field before creating duplicates
- **Reopen handling** — If a previously closed case reappears, creates a new ticket and links it to the old one

---

## Architecture

### High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Azure Function App                           │
│                     (Consumption Plan, Linux)                       │
│                                                                     │
│  ┌───────────────┐    ┌──────────────────────────────────────────┐  │
│  │ Timer Trigger  │───>│            Sync Engine                   │  │
│  │  (every 60s)   │    │                                          │  │
│  └───────────────┘    │  1. Process retry queue                  │  │
│                        │  2. Cortex -> Jira  (new/changed cases)  │  │
│  ┌───────────────┐    │  3. Check open cases (bidir closure)     │  │
│  │ HTTP Triggers  │───>│  4. Jira -> Cortex  (closed alerts)     │  │
│  │ /health /sync  │    │  5. Issue sync      (standalone)        │  │
│  │ /test          │    │  6. Prune old records                   │  │
│  └───────────────┘    └────────┬─────────────────┬───────────────┘  │
│                                │                 │                   │
│                    ┌───────────▼───┐   ┌─────────▼─────────┐        │
│                    │ Cortex Client │   │   Jira Client      │        │
│                    │ (Public API)  │   │ (REST API v3)      │        │
│                    └───────┬───────┘   └─────────┬──────────┘        │
│                            │                     │                   │
└────────────────────────────┼─────────────────────┼───────────────────┘
                             │                     │
              ┌──────────────▼──────┐   ┌──────────▼──────────┐
              │   Cortex XSIAM      │   │   Jira Cloud        │
              │                     │   │                     │
              │ • Case search       │   │ • Issue CRUD        │
              │ • Case update       │   │ • JQL search        │
              │ • Issue search      │   │ • Changelog         │
              │ • Playbook state    │   │ • User search       │
              └─────────────────────┘   │ • Assignment        │
                                        │ • Issue linking     │
                                        └─────────────────────┘

        ┌─────────────────────────────────────────────────────┐
        │                  Azure Services                      │
        │                                                      │
        │  ┌──────────────┐  ┌─────────────┐  ┌────────────┐  │
        │  │ Blob Storage  │  │  Key Vault   │  │    App     │  │
        │  │              │  │              │  │  Insights  │  │
        │  │ state.json   │  │ CORTEX_API   │  │            │  │
        │  │ (sync state) │  │ _KEY         │  │  Logs &    │  │
        │  │              │  │ JIRA_API     │  │  Metrics   │  │
        │  │              │  │ _TOKEN       │  │            │  │
        │  └──────────────┘  └─────────────┘  └────────────┘  │
        └─────────────────────────────────────────────────────┘
```

### Sync Cycle (every 60 seconds)

```
  ┌─────────────────────────────────────────────────────────────┐
  │                     SYNC CYCLE                              │
  │                                                             │
  │  ┌─────────────────────────────────────────────────────┐    │
  │  │ Phase 1: Retry Queue                                │    │
  │  │ Process any previously failed ticket creations      │    │
  │  │ (exponential backoff, max 5 attempts)               │    │
  │  └──────────────────────┬──────────────────────────────┘    │
  │                         ▼                                   │
  │  ┌─────────────────────────────────────────────────────┐    │
  │  │ Phase 2: Cortex -> Jira                             │    │
  │  │ • Fetch non-resolved cases updated since last poll  │    │
  │  │ • Filter by domain, skip pending playbooks          │    │
  │  │ • Deduplicate via custom field lookup               │    │
  │  │ • Create Jira tickets with ADF descriptions         │    │
  │  │ • Auto-assign analyst, link reopened cases          │    │
  │  └──────────────────────┬──────────────────────────────┘    │
  │                         ▼                                   │
  │  ┌─────────────────────────────────────────────────────┐    │
  │  │ Phase 3: Open Case Check                            │    │
  │  │ • Batch-fetch all tracked open cases from Cortex    │    │
  │  │ • Detect Cortex-side resolutions -> mark closed     │    │
  │  │ • Detect Jira-side closures -> resolve Cortex case  │    │
  │  │ • Sync severity changes -> update Jira priority     │    │
  │  │ • Sync newly added issues -> comment on ticket      │    │
  │  └──────────────────────┬──────────────────────────────┘    │
  │                         ▼                                   │
  │  ┌─────────────────────────────────────────────────────┐    │
  │  │ Phase 4: Jira -> Cortex                             │    │
  │  │ • Search Jira for alerts closed since last poll     │    │
  │  │ • Match to tracked sync records                     │    │
  │  │ • Resolve Cortex cases using resolution map         │    │
  │  │   (Jira pre-Done status -> Cortex resolve_reason)   │    │
  │  └──────────────────────┬──────────────────────────────┘    │
  │                         ▼                                   │
  │  ┌─────────────────────────────────────────────────────┐    │
  │  │ Phase 5: Standalone Issue Sync (optional)           │    │
  │  │ • Fetch assigned issues not linked to any case      │    │
  │  │ • Skip unassigned, resolved, pending playbooks      │    │
  │  │ • Create Jira tickets, auto-assign analyst          │    │
  │  └──────────────────────┬──────────────────────────────┘    │
  │                         ▼                                   │
  │  ┌─────────────────────────────────────────────────────┐    │
  │  │ Housekeeping                                        │    │
  │  │ • Prune closed records older than 7 days            │    │
  │  │ • Save state to Azure Blob Storage                  │    │
  │  └─────────────────────────────────────────────────────┘    │
  └─────────────────────────────────────────────────────────────┘
```

### State Schema

State is persisted as a single `state.json` blob in Azure Blob Storage. It is read at the start of each cycle and written back at the end.

```json
{
  "last_poll_ms": 1713000000000,
  "last_jira_poll_iso": "2026-04-13T10:00:00+00:00",
  "sync_records": {
    "<case_id>": {
      "jira_key": "SEC-100",
      "severity": "HIGH",
      "issue_ids": ["111", "222"],
      "status": "open",
      "created_at": "2026-04-13T10:00:00+00:00"
    }
  },
  "issue_sync_records": {
    "<issue_id>": {
      "jira_key": "SEC-101",
      "status": "open",
      "created_at": "2026-04-13T10:00:00+00:00"
    }
  },
  "retry_queue": [
    {
      "case_id": "99999",
      "case_json": "{...}",
      "attempts": 1,
      "next_retry_ms": 1713000060000
    }
  ],
  "user_cache": {
    "analyst@company.com": "jira-account-id-xxx"
  }
}
```

### Project Structure

```
jira-cortex-sync-azure/
├── function_app.py              # Azure Function entrypoint
│                                #   - Timer trigger (60s sync cycle)
│                                #   - HTTP triggers (/health, /sync, /test)
├── sync/
│   ├── config.py                # Config dataclass loaded from env vars
│   ├── state.py                 # Azure Blob Storage read/write + record pruning
│   ├── cortex_client.py         # Cortex XSIAM public API client
│   ├── jira_client.py           # Jira Cloud REST API v3 client
│   ├── adf_builder.py           # Atlassian Document Format helpers
│   ├── engine.py                # Sync orchestration, retry queue, assignment
│   └── log.py                   # Logging configuration
├── infra/
│   └── main.bicep               # Azure IaC (all resources in one template)
├── requirements.txt             # Python dependencies
├── host.json                    # Azure Functions host configuration
├── local.settings.json.example  # Local dev config template
└── params.bicep.example         # Bicep deployment parameters template
```

### Closure Resolution Flow

When a Jira ticket is closed, the engine determines the Cortex resolution reason by reading the Jira changelog:

```
Jira ticket reaches "Done" status category
         │
         ▼
Read changelog -> find last status transition
         │
         ▼
Extract "from_status" (the status before Done)
         │
         ▼
Look up from_status in RESOLUTION_TYPE_MAP
         │
    ┌────┴────┐
    │ Found   │ Not found
    ▼         ▼
Use mapped    Use DEFAULT_RESOLUTION_TYPE
reason        (default: "Resolved - Other")
    │         │
    └────┬────┘
         ▼
POST /public_api/v1/case/update/{case_id}
  status_progress: "Resolved"
  resolve_reason: <mapped reason>
  resolve_comment: "Resolved via Jira SEC-100"
```

---

## Prerequisites

| Requirement | Version | Install |
|-------------|---------|---------|
| Azure CLI | 2.50+ | [Install guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| Azure Functions Core Tools | v4 | [Install guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) |
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |

You will also need:
- An **Azure subscription** with permission to create Resource Groups, Storage Accounts, Function Apps, Key Vaults
- A **Cortex XSIAM API key** and key ID (generated in Settings > API Keys)
- A **Jira Cloud API token** for a service account (generated at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens))

---

## Deployment

### Step 1: Clone and configure

```bash
git clone https://github.com/paulharken/jira-cortex-sync-azure.git
cd jira-cortex-sync-azure
```

Copy and fill in the deployment parameters:

```bash
cp params.bicep.example params.bicep.json
```

Edit `params.bicep.json` — at minimum you need:
- `baseName` — a short name for your resources (e.g. `cortex-jira-sync`)
- `cortexBaseUrl`, `cortexApiKey`, `cortexApiKeyId`
- `jiraSiteUrl` (or `jiraCloudId`), `jiraEmail`, `jiraApiToken`, `jiraProjectKey`

### Step 2: Deploy infrastructure

```bash
az login
az group create --name cortex-jira-sync-rg --location australiaeast

az deployment group create \
  --resource-group cortex-jira-sync-rg \
  --template-file infra/main.bicep \
  --parameters @params.bicep.json
```

This creates all Azure resources in one command:

| Resource | Purpose |
|----------|---------|
| **Storage Account** | Hosts the Functions runtime and the `state.json` blob |
| **Function App** | Consumption plan (Linux, Python 3.11) — scales to zero when idle |
| **Application Insights** | Logs, metrics, and live monitoring |
| **Key Vault** | Stores `CORTEX_API_KEY` and `JIRA_API_TOKEN` as secrets |

The Function App's managed identity is automatically granted Key Vault Secrets User access.

### Step 3: Deploy function code

```bash
func azure functionapp publish <function-app-name>
```

The function app name is output from the Bicep deployment (format: `<baseName>-func`).

### Step 4: Verify

**Option A — Application Insights**: Open the Function App in the Azure portal, go to Application Insights, and check for sync logs. You should see the first cycle within 60 seconds.

**Option B — Health endpoint**:

```bash
# Get function key from Azure Portal > Function App > App Keys
curl "https://<app-name>.azurewebsites.net/api/health?code=<function-key>"
```

**Option C — Connectivity test**:

```bash
curl "https://<app-name>.azurewebsites.net/api/test?code=<function-key>"
```

Returns status for both Cortex and Jira connections.

---

## HTTP Endpoints

All endpoints require a function key passed as `?code=<key>` query parameter or `x-functions-key: <key>` header.

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/health` | Returns sync state summary: open/closed counts, last poll times, retry queue size |
| `POST` | `/api/sync` | Triggers a full sync cycle on demand, returns detailed results |
| `GET` | `/api/test` | Tests connectivity to both Cortex and Jira, returns pass/fail per service |

---

## Configuration Reference

All settings are environment variables. In Azure they are set as App Settings (the Bicep template handles this). For local dev, use `local.settings.json`.

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `CORTEX_BASE_URL` | Cortex API base URL | `https://api-yourorg.xdr.us.paloaltonetworks.com` |
| `CORTEX_API_KEY` | Cortex API key | _(stored in Key Vault)_ |
| `CORTEX_API_KEY_ID` | Cortex API key ID | `42` |
| `JIRA_EMAIL` | Jira service account email | `svc-cortex@yourorg.com` |
| `JIRA_API_TOKEN` | Jira API token | _(stored in Key Vault)_ |
| `JIRA_PROJECT_KEY` | Target Jira project | `SEC` |
| `JIRA_SITE_URL` or `JIRA_CLOUD_ID` | At least one required | `https://yourorg.atlassian.net` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CORTEX_CONSOLE_URL` | _(empty)_ | Console URL for deep links in Jira ticket descriptions |
| `CORTEX_CASE_DOMAIN` | `security` | Only sync cases from this domain |
| `JIRA_ISSUE_TYPE` | `Alert` | Jira issue type for created tickets |
| `JIRA_CASE_ID_FIELD` | _(empty)_ | Custom field ID for Cortex case ID (enables duplicate detection) |
| `JIRA_ISSUE_ID_FIELD` | _(empty)_ | Custom field ID for Cortex issue ID |
| `JIRA_XDR_URL_FIELD` | _(empty)_ | Custom field ID for XDR console URL |
| `RESOLUTION_TYPE_MAP` | _(see below)_ | JSON mapping Jira status names to Cortex resolve reasons |
| `DEFAULT_RESOLUTION_TYPE` | `Resolved - Other` | Fallback when Jira status isn't in the map |
| `MAX_SYNC_CASES` | `0` | Limit cases synced per cycle (0 = unlimited) |
| `SYNC_ISSUES` | `false` | Set to `true` to enable standalone issue sync |
| `SYNC_FROM_DATE` | _(empty)_ | Only sync cases after this ISO date (e.g. `2026-01-01`) |
| `STATE_CONTAINER_NAME` | `cortex-jira-sync` | Azure Blob container name for state file |

### Default Resolution Type Map

```json
{
  "False Positive": "Resolved - False Positive",
  "Duplicate": "Resolved - Duplicate Case",
  "Known Issue": "Resolved - Known Issue",
  "Security Testing": "Resolved - Security Testing",
  "TP Malicious": "Resolved - TP Malicious",
  "TP Benign": "Resolved - TP Benign",
  "SPAM": "Resolved - SPAM or Marketing"
}
```

Keys are Jira workflow status names (the status *before* "Done"). Values are valid Cortex `resolve_reason` strings for your tenant.

---

## Local Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in local config
cp local.settings.json.example local.settings.json
# Edit local.settings.json with your Cortex/Jira credentials

# For blob storage locally, either:
#   a) Install and start Azurite: npm install -g azurite && azurite
#   b) Use a real Azure Storage connection string in local.settings.json

# Start the function app
func start
```

The timer fires every 60 seconds. For on-demand testing:

```bash
# Manual sync
curl -X POST http://localhost:7071/api/sync

# Health check
curl http://localhost:7071/api/health

# Connectivity test
curl http://localhost:7071/api/test
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Missing required setting: CORTEX_API_KEY` | Key Vault reference not resolving | Check the Function App managed identity has Key Vault Secrets User role |
| `Cortex HTTP 401` | Bad API key or key ID | Regenerate the key in Cortex Settings > API Keys |
| `Jira HTTP 401` | Bad email/token combo | Regenerate at id.atlassian.com, ensure the email matches |
| `Jira HTTP 400` on ticket creation | Custom field format issue | Dropdown fields need `{"value": "X"}` not `"X"` — check your field types |
| Timer not firing | Function App stopped or scaling issue | Check Function App status in portal; Consumption plan cold starts can delay first run |
| State blob not found | First run or container missing | This is normal on first run — the app creates the container and starts with empty state |
| Duplicate tickets | `JIRA_CASE_ID_FIELD` not set | Set this to a custom field ID to enable duplicate detection |
