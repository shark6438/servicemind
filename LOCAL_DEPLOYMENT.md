# Local deployment

This checkout is configured to run entirely on the local machine with Ollama (`qwen2.5:1.5b`) and SQLite. No cloud model API key is required.

`NO_PROXY` is set for localhost because this Windows installation has a system proxy; without the bypass, Python's Ollama client receives a proxy-generated HTTP 502 response.

## Start

```powershell
Set-Location D:\FastAPI\agent-service-toolkit
.\start-local.ps1
```

- API documentation: http://127.0.0.1:8080/docs
- Streamlit UI: http://127.0.0.1:8501
- GLPI: http://127.0.0.1:8088
- Keycloak: http://127.0.0.1:8090
- ServiceMind PostgreSQL: `127.0.0.1:55432`

## Stop

```powershell
.\stop-local.ps1
```

Runtime logs and PID files are stored in `.runtime/`. Conversation checkpoints are stored in `data/checkpoints.db`.

To use a cloud model, set its API key in `.env`, remove the `OLLAMA_*` settings, and keep `USE_FAKE_MODEL=false`.

Use `start-servicemind.ps1` and `stop-servicemind.ps1` to start or stop the Agent service, UI, GLPI and MariaDB together.

The combined script also applies Alembic migrations, initializes LangGraph PostgreSQL schemas and idempotently seeds the Acme/Globex tenant integrations.
