# Deployment — Smart Parking Management System

Two things run this project: `scripts/serve.py` (FastAPI + the camera pipelines +
the database writer) and nginx (the built React dashboard plus the reverse proxy in
front of the API). There is no separate frontend server container — nginx serves the
bundle that `frontend-build` writes to `frontend/dist`.

The database is **SQLite** at `data/smart_parking.db`. A PostgreSQL service exists in
`docker-compose.yml`, but it sits behind `profiles: ["postgres"]`, so a normal
`up` does not start it and nothing writes to it. See
[Database](#database-sqlite-is-the-runtime-database) below before changing that.

## The three ways to run it

| | Command | URL | TLS | Use for |
|---|---|---|---|---|
| Local development | `python scripts/serve.py` + `npm run dev` | http://localhost:5173 | no | editing code, hot reload |
| Docker, plain HTTP | `docker compose -f docker-compose.yml up -d` | http://localhost | no | running the real deployment shape |
| Docker, HTTPS | `docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d` | https://localhost | **yes, if certs exist** | production |

The middle one maps port **80 and nothing else**. HTTPS is not a comment to
uncomment: it is a second Compose file that replaces nginx's entry configuration and
adds the 443 mapping. With no certificates in `deploy/ssl/` that second command fails
loudly, which is the intended behaviour — a port 443 that answers "connection refused"
is honest, while a container that maps 443 and cannot complete a handshake is a
promise the deployment cannot keep.

## Prerequisites

1. **Docker Desktop** with the WSL2 backend (Windows) or native Docker (Linux/macOS).
2. **NVIDIA Container Toolkit** if you want YOLO inference in the API container. The
   `api` service declares `runtime: nvidia`; without the toolkit the container will not
   start. `deploy/docker-compose.yml` says what to check.
3. **Docker Compose v2.24 or newer** — `docker compose version`. The TLS overlay uses
   the `!override` merge tag, introduced in that version. An older Compose rejects the
   tag out loud rather than merging the wrong thing.
4. ~8 GB free RAM for the full stack (4 GB is the API's limit).

`docker` is not on the PATH in every shell on this machine; from Git Bash it lives at
`/c/Users/<you>/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe`, and
Docker Desktop has to be running first (`npipe … dockerDesktopLinuxEngine` in an error
means it is not).

## Quick start (Docker, plain HTTP)

Run everything from the repository root. Compose takes the *directory of the compose
file* as its project directory, so `-f deploy/docker-compose.yml` reads
`deploy/.env` — not a `.env` in the repo root.

### 1. Secrets

```bash
cp deploy/.env.example deploy/.env
```

Fill `deploy/.env` in. Four variables are read, and three of them are hard
requirements (`${VAR:?…}` makes `docker compose` refuse to continue without them):

| Variable | Read by | Notes |
|---|---|---|
| `SMART_PARKING_JWT_SECRET` | the API's auth layer | 64 hex chars. `bash deploy/setup-secrets.sh` generates one into `deploy/secrets/jwt_secret.txt`; `python scripts/dev_secret.py` writes the laptop key file. **Never commit the value.** |
| `HEALTH_EMAIL`, `HEALTH_PASSWORD` | the `api` container's healthcheck | `/health` is authenticated by design, so the probe logs in instead. Give it a dedicated **VIEWER** account — see step 3. |
| `POSTGRES_PASSWORD` | the `db` service only | Only interpolated when `--profile postgres` is used. Leave the placeholder if you are not switching database. |

`API_HOST`, `API_PORT`, `LOG_LEVEL`, `ENABLE_PIPELINES`, `ENABLE_METRICS` and
`CORS_ALLOW_ORIGINS` are **not** environment variables and nothing reads them; they
were removed from `.env.example` rather than left as a fiction. Host/port/log level and
the pipeline switches are `scripts/serve.py` arguments (the compose file already passes
`--host 0.0.0.0 --persist`), and the CORS allow-list lives in `configs/app.yaml` under
`api.cors`.

`.gitignore` already excludes `deploy/.env`, `deploy/secrets/`, `deploy/backups/`,
`data/*.db*` and — added with this deployment review — `deploy/ssl/` and
`data/backups/`, which is where a pre-migration database copy lives. Those last two
patterns matter because both directories hold password hashes or private keys and
neither was covered before. A `deploy/.env` with real values exists on this machine
already; it is not in version control and it should not be moved anywhere it would be.

### 2. Migrate the database

```bash
python scripts/migrate.py upgrade head
```

The `api` container runs this itself on start (`command:` in the compose file), but
running it on the host first means a migration error shows up as a command failure
rather than as a restarting container. It resolves the URL from `configs/app.yaml`
through the same function the app uses, so a migration and a run cannot point at
different files.

### 3. Create accounts

```bash
python scripts/create_user.py --email you@example.com --role ADMIN
```

`scripts/serve.py` will not start against a database with no accounts — every endpoint
but `/auth/login` would answer 401 forever — and it says so, with this command. Create
the low-privilege probe account too, and name it in `deploy/.env`:

```bash
python scripts/create_user.py --email probe@parking.local --role VIEWER
```

### 4. Build and start

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Containers:

- `smart-parking-api` — FastAPI, GPU, `--persist` (writes the cameras' FSM transitions;
  without it every bay stays `UNKNOWN` and the dashboard shows no occupancy).
- `smart-parking-frontend-build` — a **one-shot job**, not a server: it builds React and
  writes the bundle into the host's `frontend/dist`, which nginx then serves.
- `smart-parking-nginx` — serves the dashboard, proxies `/api/`, `/ws/` and the MJPEG
  stream, rate-limits `/api/`.
- `smart-parking-db` — Postgres, **not started** unless you add `--profile postgres`.

### 5. Open it

- Dashboard: <http://localhost>
- nginx's own liveness page: <http://localhost/health> — returns `OK` from nginx, so it
  proves nginx is up and nothing more.
- The API's real health report: `GET /api/v1/health` **with a token** (it names cameras
  and error paths, so it is authenticated by design). Inside the API container it is on
  port 8000.
- Interactive API docs: <http://localhost:8000/docs>. They are **not** proxied through
  nginx on purpose — `/docs` and `/openapi.json` are served by the app at its root, and
  the proxy forwards `/api/`, `/ws/` and static files only. Public docs would hand an
  attacker the whole request surface; open them locally, or add a location to
  `deploy/nginx-site.conf` if you decide you want that exposed.

## HTTPS

TLS needs three things: a certificate, its key, and the second Compose file.

```bash
# 1. Put the files where nginx.tls.conf looks for them.
mkdir -p deploy/ssl
sudo cp /etc/letsencrypt/live/parking.example.com/fullchain.pem deploy/ssl/
sudo cp /etc/letsencrypt/live/parking.example.com/privkey.pem  deploy/ssl/

# A self-signed certificate is fine for proving the configuration works, and it is
# not fine for anything a browser is expected to trust:
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout deploy/ssl/privkey.pem -out deploy/ssl/fullchain.pem \
    -subj "/CN=localhost"

# 2. Start both files.
docker compose -f deploy/docker-compose.yml -f docker-compose.tls.yml up -d
```

`docker-compose.tls.yml` overrides only the `nginx` service, and `!override` makes its
`volumes` and `ports` lists *replace* the base ones instead of appending. That is how
`nginx.tls.conf` takes the place of `nginx.conf` at `/etc/nginx/conf.d/default.conf`
rather than sitting beside it and declaring the `upstream` and `log_format` twice. The
overlay also repoints nginx's healthcheck at `https://localhost/health`, so "healthy"
means a TLS handshake completed inside the container.

`deploy/nginx.tls.conf` redirects port 80 to 443 and listens with
`TLSv1.2`/`TLSv1.3` and `ssl_session_cache`. **`Strict-Transport-Security` ships
commented out.** Send it only once you have a certificate a browser trusts, and never
on `localhost`: HSTS from a self-signed cert pins a hostname your browser will then
refuse to load even after you fix the certificate, and the switch back to plain HTTP
will look like an outage.

If you want Let's Encrypt to renew through nginx, remember that the port-80 server in
`nginx.tls.conf` immediately redirects: use a DNS-01 challenge, or add an
`location /.well-known/acme-challenge/` block ahead of the `return 301`.

## nginx: which file does what

| File in `deploy/` | Mounted at | Level | Used by |
|---|---|---|---|
| `nginx.conf` | `/etc/nginx/conf.d/default.conf` | http | HTTP only |
| `nginx.tls.conf` | `/etc/nginx/conf.d/default.conf` | http | TLS only |
| `nginx-http.conf` | `/etc/nginx/snippets/http.conf` | http | both |
| `nginx-site.conf` | `/etc/nginx/snippets/site.conf` | server | both |
| `Dockerfile.nginx` | (build) | — | both |

Exactly one entry file is ever mounted at `/etc/nginx/conf.d/default.conf`, which is why
TLS is a whole second entry file rather than a few commented lines: nginx cannot define
a `server` block conditionally, and the image's own `nginx.conf` globs
`include /etc/nginx/conf.d/*.conf;` at **http** level — so a file full of `location`
blocks dropped in there is a startup error. That is also why the two snippets live under
`/etc/nginx/snippets/`: that directory is not globbed. `upstream`, `log_format` and
`limit_req_zone` belong at http level; `location` belongs inside a `server`.

Both variants share `nginx-site.conf`, so routing is written once: `/api/` (rate-limited,
burst 20), the `stream.mjpg` location (unbuffered, no `limit_req` — a buffered multipart
response never reaches an `<img>` tag), `/ws/` (upgrade headers, 24 h read timeout), the
four security headers, and the SPA `try_files` with a year of asset caching. Every
`proxy_pass` is written as `http://api_backend;` **with no URI**, because that is what
forwards the original query string; adding a path would silently strip it and break the
socket-ticket handshake.

`deploy/nginx-frontend.conf` is left over from the earlier layout and is referenced by
nothing. It is not mounted anywhere; it can be deleted once you have confirmed that.

### Credentials in logs

A WebSocket handshake from a browser cannot carry an `Authorization` header, so the
socket's URL carries a **one-time ticket** from `POST /auth/ws-ticket` (15 s TTL, single
use) as `?ticket=`, and the MJPEG `<img>` tag can only carry `?token=`. Both are
credentials, and both would otherwise be copied into logs. Three layers close that:

1. `nginx-http.conf` defines `log_format sp_no_query` with `escape=default` and logs
   `$request_method $uri $server_protocol` — `$uri` excludes the query string, and
   `escape=default` stops a crafted URL from forging a log line.
2. The two locations that can carry a credential set
   `error_log /var/log/nginx/error.log crit;`. Upstream failures are the one nginx
   message that quotes the *entire* request line with its query string, plus the
   upstream URL, regardless of `log_format`.
3. `src/smart_parking/api/access_log.py` masks `ticket=` and `token=`, wired into
   uvicorn's logging by `scripts/serve.py` through `log_config`. It is attached to
   `uvicorn.access`, `uvicorn.error` **and** `uvicorn`, and the middle one is the
   reason: uvicorn logs a WebSocket handshake itself, on `uvicorn.error`, as
   `127.0.0.1:50853 - "WebSocket /ws/live?ticket=…" [accepted]`. A filter on the access
   logger alone masked the dashboard's ordinary API calls and left every socket's
   ticket in the file - checked by running a server, connecting, and grepping what it
   wrote. This matters because `serve.py` runs without nginx in front of it in local
   development, and inside the API container, whose output
   `docker compose logs api` keeps.

Verify it on a running stack:

```bash
docker compose -f deploy/docker-compose.yml logs api | grep -cE 'ticket=[A-Za-z0-9_-]{8,}'   # want 0
docker compose -f deploy/docker-compose.yml exec nginx sh -c \
  'grep -cE "ticket=[A-Za-z0-9_-]{8,}" /var/log/nginx/*.log'                                  # want 0
```

`deploy/docker-compose.yml` deliberately still publishes `8000:8000`. That is a
development convenience and a security hole at the same time: a client that reaches the
API directly bypasses nginx's rate limit and the `sp_no_query` log format. For a real
deployment, drop that mapping (or bind it to `127.0.0.1:8000:8000`) and rely on the
proxy.

## Database: SQLite is the runtime database

The URL comes from one place, `core.config.database_url_for`, in a fixed precedence: an
explicit argument (`--url`, a test's scratch file) wins, then
`SMART_PARKING_DATABASE_URL`, then `database.url` in `configs/app.yaml` — which is
`sqlite:///data/smart_parking.db`. A **relative** sqlite path is resolved against the
project root, never the working directory, so starting the server from another folder
cannot quietly create a second database beside it. The `api` container sets
`SMART_PARKING_DATABASE_URL` to exactly the same file, and `../data` is host-mounted, so
records survive a rebuild. `tests/unit/test_db_schema.py` asserts that the compose value
and the YAML default still name the same file.

If a sqlite file named by the URL does not exist yet, `serve.py` and `migrate.py` print
which path they are about to create and where that decision came from, *before* creating
it. The old failure mode was silent: compose advertised Postgres through a `DATABASE_URL`
variable nothing read, while the API wrote SQLite.

### Migrations

```bash
python scripts/migrate.py current              # what revision this file is on
python scripts/migrate.py check                # models vs database: is a revision needed?
python scripts/migrate.py upgrade head
python scripts/migrate.py --url sqlite:///data/scratch.db downgrade base
```

`0003_logout_epoch.py` adds `users.tokens_invalid_before` (the server-side revocation
epoch that makes logout invalidate a whole session) and rebuilds `audit_logs`' CHECK
constraint to admit the `logout` action. **The database this project runs against is at
`0003` as of 2026-10-13**; a pre-migration copy is kept at
`data/backups/smart_parking_pre0003_20261013.db`. Logout and token revocation do not
work against a `0002` file, so run `upgrade head` on any database you point at.

### Backup and restore

`deploy/backup.sh` runs `pg_dump` against the `db` container. With the default SQLite
runtime that container is not even started, so **it does not back up your data** — it
fails, or backs up an empty Postgres. The database that holds your visits is a file:

```bash
# Consistent snapshot of a live SQLite database (folds the WAL in).
python -c "import sqlite3; s=sqlite3.connect('data/smart_parking.db'); \
  d=sqlite3.connect('data/backups/parking_$(date +%Y%m%d_%H%M%S).db'); \
  s.backup(d); d.close(); s.close()"
```

`deploy/restore.sh` is Postgres-only for the same reason, and it drops and recreates the
database. For SQLite, stop the API, replace the file with a verified backup, and start
again. Write a `backup_sqlite.sh` (or point `backup.sh` at the real backend) before
relying on either script in production — `deploy/backups/` currently holds no SQLite
snapshot.

## Logs

```bash
docker compose -f deploy/docker-compose.yml logs -f api
docker compose -f deploy/docker-compose.yml logs -f nginx
docker compose -f deploy/docker-compose.yml exec nginx sh -c \
    'tail -n 50 /var/log/nginx/access.log'
```

A deployed dashboard is same-origin through nginx, so **CORS is not part of the
development loop and not part of the deployment** — the browser calls its own origin, and
`configs/app.yaml` ships an empty allow-list and stays that way. If you see a CORS error
in the console you are calling port 8000 directly; use port 80 (or the Vite dev server,
which proxies). Set `api.cors.allow_origins` only for a client you have decided to let
talk to the API cross-origin.

## Updating the application

```bash
docker compose -f deploy/docker-compose.yml down
docker compose -f deploy/docker-compose.yml up -d --build
python scripts/migrate.py upgrade head
```

The API container mounts `../src` read-only over the copy baked into the image and sets
`PYTHONPATH=/app/src`, so a code change deploys with a container restart. Rebuilding the
image is only needed when dependencies change — and its `pip` layer needs a network this
machine does not always have.

## Troubleshooting

**`docker compose config` says `SET HEALTH_EMAIL` / `POSTGRES_PASSWORD`** — those keys are
missing from `deploy/.env`. `POSTGRES_PASSWORD` only matters with `--profile postgres`.

**nginx exits with `cannot load certificate … No such file or directory`** — you started
the TLS overlay without certificates in `deploy/ssl/`. Either add them or drop
`-f docker-compose.tls.yml`.

**nginx exits with `"location" directive is not allowed here`** — a server-level file was
dropped into `/etc/nginx/conf.d/` instead of `/etc/nginx/snippets/`. Check the mounts.

**The API container restarts in a loop** — `docker compose logs api`. Usual causes: GPU
unavailable (`runtime: nvidia`), `SMART_PARKING_JWT_SECRET` unset, database missing
tables (run `migrate.py upgrade head`), no accounts (run `create_user.py`).

**The dashboard shows every bay as `UNKNOWN`** — persistence is not running, or the
cameras have not completed a debounce window yet. `UNKNOWN` is a real state and the
dashboard is not allowed to paint it as available; check `--persist` on the API command
and `/api/v1/health`'s camera states.

**The live view says offline and will not reconnect** — a fatal close means the server
rejected the identity, not the network: `token_revoked` (someone logged out, or an admin
deactivated the account), `user_inactive`, `user_unknown`, `role_changed`. Log in again.
A reconnectable close (`ticket_required`, `token_invalid`) retries with a fresh ticket on
its own.

## Soak test

```bash
bash deploy/soak-test.sh 24      # or a shorter 2
```

Checks API and database health every 60 s, records memory snapshots, and at hours 6, 12
and 18 restarts services to prove recovery. Pass criteria: zero failures over the window.
Note that its health polling writes an audit row per login, which is why `audit_logs` can
fill with `login` entries on a machine that has been running the stack for a while.

## Security checklist

- [ ] `deploy/.env` exists with real values and is **not** committed (`deploy/secrets/`
      and `deploy/ssl/` likewise)
- [ ] `SMART_PARKING_JWT_SECRET` is 64+ hex characters and not shared with another system
- [ ] The healthcheck account is `VIEWER`, not an admin
- [ ] HTTPS started only with a trusted certificate; HSTS uncommented only then
- [ ] Port `8000` not published to the network in the production overlay
- [ ] `configs/app.yaml` `api.cors.allow_origins` still empty unless deliberately changed
- [ ] Migrations applied (`python scripts/migrate.py check` says nothing new is needed)
- [ ] Backups actually capture the file the app writes (see [Backup](#backup-and-restore))
- [ ] nginx access and error logs contain no `ticket=` or `token=` values:
      `docker compose exec nginx sh -c 'grep -cE "ticket=[^* ]|token=[^* ]" /var/log/nginx/*.log' || true`
- [ ] Containers run as non-root (the API image already does)
