# ServiceMind GLPI development stack

This stack runs the real GLPI application and MariaDB locally for the first ServiceMind integration milestone.

- GLPI version: `11.0.8`
- MariaDB version: `11.8`
- GLPI URL: `http://127.0.0.1:8088`
- Database port: internal only
- Persistent data: Docker named volumes

## Commands

```powershell
Set-Location D:\FastAPI\agent-service-toolkit\deploy\glpi
docker compose up -d
docker compose ps
docker compose logs --tail 100 glpi
```

Stop the stack without deleting data:

```powershell
docker compose stop
```

Do not use `docker compose down --volumes` unless the GLPI and MariaDB data should be permanently deleted.

The initial GLPI administrator account is only for local bootstrap. Change the default password before exposing the service beyond localhost.
