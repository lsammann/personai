# Email Agent

A personal agent that watches my Gmail inbox and sorts mail into labelled
buckets using a locally-run LLM (Ollama).

- **[DESIGN.md](DESIGN.md)** — what it does and why every decision was made
- **[docs/PLAN.md](docs/PLAN.md)** — phased build order and gates

Read-only: the OAuth scope is `gmail.readonly` and stays that way until
Phase 3, which makes damaging the inbox structurally impossible for the first
half of the build rather than merely unlikely.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and [Ollama](https://ollama.com).

```bash
uv sync
ollama pull llama3.1:8b
```

### Google Cloud

1. Create a project and enable the **Gmail API**
2. Configure the OAuth consent screen (External, add yourself as a test user)
3. Create an OAuth client ID of type **Web application** with the redirect URI
   `http://localhost:8000/auth/callback`
4. Download the JSON as `data/credentials.json`

The consent screen stays in *Testing* status, so the refresh token expires
weekly and re-authentication is a routine event — see DESIGN.md.

### Authenticate

```bash
uv run uvicorn app.main:app --port 8000
```

Open <http://localhost:8000> and click through. Expect an "unverified app"
warning: Advanced → Go to email-agent.

## Secrets

`data/` is gitignored and holds `credentials.json`, `token.json`, and local
state. Nothing in it is ever committed.
