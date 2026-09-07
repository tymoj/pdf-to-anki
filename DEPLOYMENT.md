# Deployment

The bot runs as a Docker Compose stack (bot + MinIO) on a homelab server. GitHub
Actions builds and publishes the image; watchtower on the server pulls and
restarts it — CI never touches the server directly.

## Architecture

1. `git push` to `main` triggers `.github/workflows/deploy.yml` (**build-push**):
   builds a multi-arch (amd64/arm64) image from `Dockerfile` and pushes it to
   `ghcr.io/tymoj/pdf-to-anki:latest` (and `:<sha>`).
2. watchtower, running on the server, polls GHCR for a new `:latest` digest and
   `docker compose pull`s + recreates the `bot` (and `telegram-bot-api`)
   containers on its own.

The server never builds the image itself — it only pulls what CI already built.
A `docker-compose.yml` change does **not** roll out this way, since watchtower
only reacts to a new image, not a file change — see "Changing app
configuration" below for how to push one by hand.

## One-time setup

### 1. Create the GitHub repo

```bash
# on github.com: create an empty private repo, e.g. tymoj/pdf-to-anki
git remote add origin git@github.com:tymoj/pdf-to-anki.git
git push -u origin main
```

### 2. Create a GHCR personal access token

CI needs no server access at all — it only pushes to GHCR using the built-in
`GITHUB_TOKEN`. This PAT is instead for the *server*, so it can
`docker login ghcr.io` and pull the (private) image itself, whether that pull
is done by watchtower or by hand. GitHub only allows creating tokens through
the web UI (no API), so this step is always manual:

1. https://github.com/settings/tokens/new
2. Scopes: `read:packages`
3. Generate and copy the token
4. On the server: `sudo docker login ghcr.io -u <your-github-username>` and
   paste the token as the password

Re-run that `docker login` whenever the token expires or is rotated — nothing
does this automatically now that CI doesn't touch the server.

### 3. Get Telegram API credentials for the local Bot API server

The stack runs a local Telegram Bot API server (see the `telegram-bot-api`
service in `docker-compose.yml`) so uploads/downloads aren't capped at
Telegram's public 20 MB/50 MB limits. It needs an `api_id`/`api_hash` pair,
which is separate from the bot token:

1. https://my.telegram.org — log in with a personal Telegram account (not the
   bot)
2. "API development tools"
3. Create an application (any name/platform is fine)
4. Copy the `api_id` and `api_hash`

Like every other app config value, these live only in the server's `.env` —
seed them by hand in the next step. (An earlier version of this workflow wrote
them in from GitHub secrets on every deploy; now that watchtower handles
rollout and CI never touches the server, that mechanism is gone, so there's no
more exception to the ".env-only" rule.)

The workflow needs no GitHub repo secrets at all — `build-push` authenticates
to GHCR with the built-in `GITHUB_TOKEN`, and nothing else in it reaches the
server.

### 4. Seed the server

The project directory and its `.env` are created once, by hand — there's no
bootstrap script for this project (unlike `presents`, it needs no Traefik routing
or public HTTP endpoint, since the bot only makes outbound connections to
Telegram and Claude).

```bash
ssh <user>@<server> 'mkdir -p /opt/homelab/projects/pdf-to-anki'
scp docker-compose.yml <user>@<server>:/opt/homelab/projects/pdf-to-anki/
```

Create `/opt/homelab/projects/pdf-to-anki/.env` on the server (see
`.env.example` for the full list), including `TELEGRAM_API_ID`/
`TELEGRAM_API_HASH` from step 3 — `docker-compose.yml` requires them to be
present the moment anything runs `docker compose up`. Generate real MinIO
credentials — never reuse the `minioadmin`/`minioadmin` dev defaults in
production:

```bash
openssl rand -hex 8        # -> S3_ACCESS_KEY
openssl rand -base64 24 | tr -d '/+='   # -> S3_SECRET_KEY
```

Then lock it down and bring the stack up:

```bash
ssh <user>@<server> 'chmod 600 /opt/homelab/projects/pdf-to-anki/.env'
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose up -d'
```

## Ongoing deploys

Push to `main` and CI publishes a new `:latest` image; watchtower on the
server notices and rolls it out on its own, no manual step needed. This does
*not* cover a `docker-compose.yml` change — copy it over and recreate the
stack by hand:

```bash
scp docker-compose.yml <user>@<server>:/opt/homelab/projects/pdf-to-anki/
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose up -d'
```

To force a redeploy without a code change (e.g. after editing the server's
`.env`), SSH in and recreate the bot:

```bash
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose up -d bot'
```

## Changing app configuration (e.g. the Telegram allowlist)

App config, `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` included, lives only in the
server's `.env` — there is no GitHub-secret equivalent to edit instead, since
the workflow doesn't read app config at all. To change a value:

```bash
ssh <user>@<server> "sed -i 's/^TELEGRAM_ALLOWED_USERNAMES=.*/TELEGRAM_ALLOWED_USERNAMES=alice,bob/' /opt/homelab/projects/pdf-to-anki/.env"
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose up -d bot'
```

Same pattern for the cleanup model, e.g. switching to Haiku 4.5:

```bash
ssh <user>@<server> "sed -i 's/^CLAUDE_MODEL=.*/CLAUDE_MODEL=claude-haiku-4-5-20251001/' /opt/homelab/projects/pdf-to-anki/.env"
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose pull && sudo docker compose up -d'
```

## Verify

```bash
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose ps'
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose logs bot --tail 50'
```

A healthy start logs, in order: the allowlist size, `storage ready`, and
`Application started`. `docker compose ps` should also show a
`telegram-bot-api` container running — it has no explicit healthcheck, just
runs.

## Troubleshooting

See the [Troubleshooting](README.md#troubleshooting) section in the README for
bot/worker-level issues. Deploy-specific ones:

- **New image isn't rolling out after a push.** CI only publishes to GHCR now
  — check watchtower's own logs on the server for pull/restart errors, not the
  GitHub Actions run (a green `build-push` just means the image was pushed,
  not that the server picked it up). The server is at `192.168.1.172`, user
  `deepthinker`, port 22, if you need to check by hand:
  `ssh deepthinker@192.168.1.172 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose pull && sudo docker compose up -d'`.
- **`docker compose pull` fails on the server (manually or via watchtower).**
  The GHCR PAT from step 2 may have expired or been revoked — re-run
  `docker login ghcr.io` on the server with a fresh one. Nothing does this
  automatically; CI no longer reaches the server at all.
- **Bot container restarts in a loop.** `docker compose logs bot` — almost
  always a missing/invalid value in the server's `.env`.
- **Bot container fails to start, or logs show it can't reach the Telegram
  API.** Check `docker compose logs telegram-bot-api` for auth errors — usually
  a wrong or missing `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` in `.env`. Also note
  that container's first boot can take a few seconds to authenticate with
  Telegram before the bot can use it.
- **`telegram.error.BadRequest: File is too big` even on a file under 2000
  MB.** The `telegram-bot-api` service isn't actually running in `--local`
  mode (`TELEGRAM_LOCAL=1`) — without it, a self-hosted server still enforces
  the public API's 20 MB/50 MB caps. If it *is* in local mode, this instead
  means the bot can't read the file off the shared `telegram-bot-api-data`
  volume: local mode makes `getFile` return a path on the `telegram-bot-api`
  container's own filesystem rather than a URL, and that server always writes
  as its fixed internal uid/gid (`101:101` in the `aiogram/telegram-bot-api`
  image) — which is why the `bot` service in `docker-compose.yml` runs as
  that same uid. If a future image version changes that uid, `docker exec
  <telegram-bot-api container> id telegram-bot-api` shows the current one to
  match `bot`'s `user:` setting against.
