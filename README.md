# Jira Cortex Sync - Azure Speed Edition

Bidirectional sync between Palo Alto Cortex XSIAM and Atlassian Jira Cloud, running as an Azure Function App.

## What it does

- Syncs Cortex XSIAM cases to Jira tickets (every 60 seconds)
- Syncs severity changes bidirectionally
- Closes Cortex cases when Jira tickets reach Done (maps Jira workflow status to Cortex resolve reason)
- Closes Jira tracking when Cortex cases are resolved
- Syncs standalone assigned Cortex issues to Jira (optional)
- Auto-assigns Jira tickets to the analyst assigned in Cortex
- Retry queue with exponential backoff for failed ticket creations
- Duplicate detection via custom field lookup

## Prerequisites

- **Azure CLI** (`az`) — [Install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- **Azure Functions Core Tools v4** (`func`) — [Install](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- **Python 3.11+**
- An Azure subscription with permission to create resources
- Cortex XSIAM API key + key ID
- Jira Cloud API token for a service account

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/paulharken/jira-cortex-sync-azure.git
cd jira-cortex-sync-azure
```

Copy the parameter templates:

```bash
cp params.bicep.example params.bicep.json
cp local.settings.json.example local.settings.json
```

Edit `params.bicep.json` with your Cortex and Jira credentials.

### 2. Deploy Azure infrastructure

```bash
az login
az group create --name cortex-jira-sync-rg --location australiaeast

az deployment group create \
  --resource-group cortex-jira-sync-rg \
  --template-file infra/main.bicep \
  --parameters @params.bicep.json
```

This creates: Storage Account, Function App (Consumption plan), Application Insights, Key Vault with secrets.

### 3. Deploy the function code

```bash
func azure functionapp publish <function-app-name>
```

The function app name is output from the Bicep deployment (e.g. `cortex-jira-sync-func`).

### 4. Verify

Check Application Insights for sync logs, or hit the health endpoint:

```bash
# Get the function key from the Azure portal or CLI
curl "https://<function-app-name>.azurewebsites.net/api/health?code=<function-key>"
```

## HTTP Endpoints

All endpoints require a function key (passed as `?code=` query param or `x-functions-key` header).

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/health` | Sync status and state summary |
| POST | `/api/sync` | Trigger a sync cycle on demand |
| GET | `/api/test` | Test connectivity to Cortex and Jira |

## Configuration Reference

All settings are environment variables (set via Azure App Settings or `local.settings.json`).

### Required

| Variable | Description |
|----------|-------------|
| `CORTEX_BASE_URL` | Cortex API base URL (e.g. `https://api-yourorg.xdr.us.paloaltonetworks.com`) |
| `CORTEX_API_KEY` | Cortex API key |
| `CORTEX_API_KEY_ID` | Cortex API key ID |
| `JIRA_EMAIL` | Jira service account email |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_PROJECT_KEY` | Jira project key (e.g. `SEC`) |
| `JIRA_SITE_URL` or `JIRA_CLOUD_ID` | At least one must be set |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CORTEX_CONSOLE_URL` | _(empty)_ | Console URL for deep links in Jira descriptions |
| `CORTEX_CASE_DOMAIN` | `security` | Case domain filter |
| `JIRA_ISSUE_TYPE` | `Alert` | Jira issue type name |
| `JIRA_CASE_ID_FIELD` | _(empty)_ | Custom field ID for Cortex case ID (enables duplicate detection) |
| `JIRA_ISSUE_ID_FIELD` | _(empty)_ | Custom field ID for Cortex issue ID |
| `JIRA_XDR_URL_FIELD` | _(empty)_ | Custom field ID for XDR console URL |
| `RESOLUTION_TYPE_MAP` | _(see code)_ | JSON: Jira status name -> Cortex resolve reason |
| `DEFAULT_RESOLUTION_TYPE` | `Resolved - Other` | Fallback resolve reason |
| `MAX_SYNC_CASES` | `0` | Max cases per cycle (0 = unlimited) |
| `SYNC_ISSUES` | `false` | Enable standalone issue sync |
| `SYNC_FROM_DATE` | _(empty)_ | Only sync cases after this date (ISO format) |
| `STATE_CONTAINER_NAME` | `cortex-jira-sync` | Blob container for state persistence |

## Local Development

```bash
# Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start Azurite for local blob storage (optional, or use a real storage account)
# Then edit local.settings.json with your credentials

# Run locally
func start
```

The timer trigger fires every 60 seconds. You can also trigger a manual sync:

```bash
curl -X POST http://localhost:7071/api/sync
```

## Architecture

```
Timer (60s) ──> function_app.py ──> sync/engine.py (orchestration)
                                       ├── sync/cortex_client.py (Cortex API)
                                       ├── sync/jira_client.py (Jira API)
                                       ├── sync/adf_builder.py (Jira descriptions)
                                       └── sync/state.py (Azure Blob persistence)
```

State is a single JSON blob in Azure Blob Storage, containing sync records, retry queue, and user cache.
