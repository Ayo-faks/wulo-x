# Docker Compose Troubleshooting

The composed stack is started with:

```bash
docker compose --project-directory . -f devops/docker-compose.yml up --build
```

It needs a configured `.env` first — see the [Full Application Configuration](https://github.com/Ayo-faks/wulo-x/blob/main/README.md#full-application-configuration) section of the README. Most first-run failures come from one of three things: missing/placeholder env values, port collisions, or services that stay unhealthy while their dependencies are still starting.

---

## 1. Missing or placeholder env values

**Symptom:** services crash-loop with `KeyError`, `ValueError`, or `ImproperlyConfigured`-style messages, or a service logs "missing value for X".

**Fix:**
1. Make sure `.env` exists at the project root: `cp .env.example .env` (or `devops/.env.example` if that is where the template lives).
2. Fill in **every** value — a placeholder like `changeme`, `your-token-here`, or an empty string will still fail.
3. Restart with a clean rebuild so the values are re-read:

   ```bash
   docker compose --project-directory . -f devops/docker-compose.yml down
   docker compose --project-directory . -f devops/docker-compose.yml up --build -d
   ```

**Tip:** if the README lists a required variable and you are unsure, search the compose file for `${VAR}` references — each one needs a corresponding value in `.env`.

---

## 2. Port collisions

**Symptom:** `Error response from daemon: driver failed programming external connectivity on endpoint ... Bind for 0.0.0.0:PORT failed: port is already allocated`.

**Fix:**
1. Find what is using the port:

   ```bash
   # Linux/macOS
   lsof -i :PORT
   # Windows (PowerShell)
   netstat -ano | findstr :PORT
   ```

2. Either stop the conflicting process, or remap the port in `devops/docker-compose.yml` (e.g. change `"8080:8080"` to `"8081:8080"`).

---

## 3. Services stay unhealthy while dependencies start

**Symptom:** `healthcheck` reports `unhealthy`, or a service depends on another that is still starting and logs connection-refused errors.

**Fix:**
1. This is often **transient** — the database/app takes longer to become ready on first run. Give it time:

   ```bash
   docker compose --project-directory . -f devops/docker-compose.yml ps
   ```

2. Check the logs of the unhealthy service:

   ```bash
   docker compose --project-directory . -f devops/docker-compose.yml logs -f <service-name>
   ```

3. If it keeps failing, check whether a **dependency** (database, cache) is actually ready:

   ```bash
   docker compose --project-directory . -f devops/docker-compose.yml logs <dependency-service>
   ```

4. On first run, a fresh database may need migrations before the app can connect — follow the README's init/migration step if one exists.

---

## 4. Nothing is running at all

**Symptom:** `up` exits immediately, or `docker compose ps` shows no containers.

**Fix:**
- Check the compose file parses: `docker compose --project-directory . -f devops/docker-compose.yml config -q`
  (no output = valid; errors will be printed).
- Check Docker itself is healthy: `docker info`
- Look at the very first logs for the real cause:

  ```bash
  docker compose --project-directory . -f devops/docker-compose.yml up --build  # foreground, watch the error
  ```

---

## Quick checklist

- [ ] `.env` exists and has no placeholder values
- [ ] No other process is using the mapped ports
- [ ] Dependency services are healthy before the app starts
- [ ] First-run migrations/init steps from the README were followed
- [ ] `docker compose ... config -q` passes (compose file is valid)
