# ⚡ PP Agent — Setup Guide

Power Platform AI Agent with a full browser UI. Supports 15+ PP tools,
multi-LLM (MiniMax M2.7 default), and works with plain-English prompts.

---

## 🚀 Quick Start (macOS)

1. **Double-click `setup.command`** — installs dependencies and creates `.env`
2. **Edit `.env`** — fill in your credentials (see below)
3. **Double-click `launch.command`** — starts the UI at http://localhost:5005

---

## 📋 Step-by-Step Setup

### Step 1 — Python 3.11+
Make sure Python 3.11+ is installed:
```bash
python3 --version
```
If not: download from https://python.org

### Step 2 — Azure App Registration

You need a free Azure AD App Registration to authenticate to Power Platform.

1. Go to https://portal.azure.com → **Entra ID → App registrations → New registration**
2. Name: `PP Agent`
3. Supported account types: **Accounts in this organizational directory only**
4. Redirect URI: `https://login.microsoftonline.com/common/oauth2/nativeclient`
5. Click **Register**
6. Copy the **Application (client) ID** → paste as `PP_CLIENT_ID` in `.env`
7. Copy the **Directory (tenant) ID** → paste as `PP_TENANT_ID` in `.env`

**Add API Permissions** (click "Add a permission"):
- **Dynamics CRM** → Delegated → `user_impersonation`
- **Power Automate Service** → Delegated → `Flows.Read.All`, `Flows.Manage.All`
- **PowerApps Service** → Delegated → `User`
- **Azure Service Management** → Delegated → `user_impersonation`

Click **Grant admin consent**.

### Step 3 — Find Your Environment Details

**Environment URL** (`PP_ENV_URL`):
- Go to https://admin.powerplatform.microsoft.com
- Click your environment → **Settings** → copy the URL (e.g. `https://yourorg.crm11.dynamics.com`)

**Environment ID** (`PP_ENV_ID`):
- Same page → **Details** → copy the **Environment ID** (a GUID)

### Step 4 — LLM API Key

Add at least one LLM key to `.env`:

| Provider | Where to get key |
|----------|-----------------|
| **MiniMax** (default) | https://platform.minimax.io/user-center/payment/token-plan |
| Claude | https://console.anthropic.com/settings/keys |
| OpenAI | https://platform.openai.com/api-keys |
| Gemini | https://aistudio.google.com/apikey |
| OpenRouter | https://openrouter.ai/keys |

**MiniMax Token Plan**: Keys start with `sk-cp-`. Gives 1,500 requests per 5 hours.

### Step 5 — Azure DevOps (Optional)

Only needed for the Azure DevOps tab.

1. Go to https://dev.azure.com → your org
2. **User Settings → Personal Access Tokens → New Token**
3. Scopes: `Work Items (Read & Write)`, `Build (Read & Execute)`, `Code (Read)`
4. Copy token → `ADO_PAT` in `.env`
5. Set `ADO_ORG` to your org name and `ADO_PROJECT` to your project

---

## 🔐 First Login

When you first use a Power Platform tool, the agent will show a **device code login**:

```
══════════════════════════════════════
  🔐  Sign in to Power Platform
══════════════════════════════════════
  1. Open:  https://microsoft.com/devicelogin
  2. Enter: ABCD1234
  Waiting for you to sign in…
```

After signing in, the token is cached. You won't be asked again unless it expires.

---

## 🔄 Switching Environments

Click the **environment badge** in the top bar to switch Dataverse environments at runtime.
Or edit `PP_ENV_URL` in `.env` and restart.

## 🤖 Switching Models

Click the **model button** (shows current model) in the top bar to open the model picker.

---

## 📁 File Structure

```
pp-agent/
├── app.py              ← Flask web UI (start here)
├── agent.py            ← Agentic brain (LLM + tool loop)
├── tools/
│   ├── auth.py         ← MSAL authentication
│   ├── llm_provider.py ← Multi-LLM provider
│   ├── solution.py     ← Solution lifecycle
│   ├── dataverse.py    ← Dataverse Web API
│   ├── flows.py        ← Power Automate
│   ├── canvas.py       ← Canvas Apps
│   ├── mda.py          ← Model-Driven Apps
│   ├── copilot.py      ← Copilot Studio
│   ├── pages.py        ← Power Pages
│   ├── powerbi.py      ← Power BI
│   ├── fabric.py       ← Microsoft Fabric
│   ├── crm.py          ← D365 CRM
│   ├── plugins.py      ← Plugin assemblies
│   ├── connectors.py   ← Custom Connectors
│   ├── environments.py ← Environment lifecycle
│   ├── security.py     ← Security Roles + DLP
│   ├── monitor.py      ← Health + Audit
│   ├── ado.py          ← Azure DevOps
│   ├── knowledge.py    ← Knowledge base
│   └── memory.py       ← Persistent memory
├── .env                ← Your credentials (never commit)
├── .env.example        ← Template
├── requirements.txt
├── launch.command      ← Double-click to start (macOS)
└── setup.command       ← Double-click to install (macOS)
```

---

## 🛠️ Running Manually

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env

# Start the web UI
python app.py
```

Open http://localhost:5005

---

## ❓ Troubleshooting

**"PP_CLIENT_ID not set"** → Edit `.env` and add your Azure App Registration client ID.

**"Device flow failed"** → Your app registration may be missing the redirect URI.
Add: `https://login.microsoftonline.com/common/oauth2/nativeclient`

**"No LLM API key found"** → Add at least `MINIMAX_API_KEY` or `ANTHROPIC_API_KEY` to `.env`.

**"PP_ENV_ID not set"** → Find it in the Power Platform Admin Center (Environment → Details).
