# Deployment

The bot runs as a Docker Compose stack (bot + MinIO) on a homelab server, built and
redeployed automatically by GitHub Actions on every push to `main` — the same setup
used for the `presents` project.

## Architecture

1. `git push` to `main` triggers `.github/workflows/deploy.yml`.
2. **build-push**: builds a multi-arch (amd64/arm64) image from `Dockerfile` and
   pushes it to `ghcr.io/tymoj/pdf-to-anki:latest` (and `:<sha>`).
3. **deploy**: copies the repo's `docker-compose.yml` to the server (so a
   compose-file change actually takes effect, not just an image change), SSHes
   in, `docker compose pull`s the new image, and `docker compose up -d`s the
   stack.

The server never builds the image itself — it only pulls what CI already built.

## One-time setup

### 1. Create the GitHub repo

```bash
# on github.com: create an empty private repo, e.g. tymoj/pdf-to-anki
git remote add origin git@github.com:tymoj/pdf-to-anki.git
git push -u origin main
```

### 2. Generate a dedicated deploy key

Don't reuse your personal SSH key — generate one just for CI to use:

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-deploy@pdf-to-anki" -f ./deploy_key
```

Install the public half on the server:

```bash
ssh <user>@<server> 'cat >> ~/.ssh/authorized_keys' < ./deploy_key.pub
```

### 3. Create a GHCR personal access token

GitHub only allows creating tokens through the web UI (no API), so this step is
always manual:

1. https://github.com/settings/tokens/new
2. Scopes: `read:packages`, `write:packages`
3. Generate and copy the token

### 4. Get Telegram API credentials for the local Bot API server

The stack runs a local Telegram Bot API server (see the `telegram-bot-api`
service in `docker-compose.yml`) so uploads/downloads aren't capped at
Telegram's public 20 MB/50 MB limits. It needs an `api_id`/`api_hash` pair,
which is separate from the bot token:

1. https://my.telegram.org — log in with a personal Telegram account (not the
   bot)
2. "API development tools"
3. Create an application (any name/platform is fine)
4. Copy the `api_id` and `api_hash`

Unlike the rest of app config, these two *are* set as GitHub secrets (see the
next step) — the deploy workflow writes them into the server's `.env` on every
deploy, so the server never needs them seeded by hand. This is a deliberate
exception to the "app config lives only in `.env`" rule below, made because
these credentials rarely change and it removes a manual seeding step; every
other app config value stays `.env`-only.

### 5. Set the GitHub repo secrets

At `github.com/<owner>/pdf-to-anki/settings/secrets/actions`:

| Secret | Value |
| --- | --- |
| `SERVER_HOST` | server IP/hostname |
| `SERVER_USER` | SSH username on the server |
| `SSH_PRIVATE_KEY` | contents of `deploy_key` (the private half from step 2) |
| `GHCR_TOKEN` | the PAT from step 3 |
| `TELEGRAM_API_ID` | the `api_id` from step 4 |
| `TELEGRAM_API_HASH` | the `api_hash` from step 4 |

```bash
gh secret set SERVER_HOST --repo <owner>/pdf-to-anki --body "<host>"
gh secret set SERVER_USER --repo <owner>/pdf-to-anki --body "<user>"
gh secret set SSH_PRIVATE_KEY --repo <owner>/pdf-to-anki < ./deploy_key
gh secret set GHCR_TOKEN --repo <owner>/pdf-to-anki   # paste when prompted
gh secret set TELEGRAM_API_ID --repo <owner>/pdf-to-anki     # paste when prompted
gh secret set TELEGRAM_API_HASH --repo <owner>/pdf-to-anki   # paste when prompted
```

Delete the local `deploy_key`/`deploy_key.pub` files once they're set — the private
key isn't needed anywhere after this.

**Note:** these are the only secrets the workflow reads. App config
(`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, …) is *not* a GitHub secret — see
below. `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are the one exception: the
workflow reads them and writes them into the server's `.env` on every deploy
(see `.github/workflows/deploy.yml`), instead of them being seeded by hand.

### 6. Seed the server

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
`TELEGRAM_API_HASH` from step 4 — `docker-compose.yml` requires them to be
present the moment anything runs `docker compose up`, and the workflow only
writes them in from the deploy step onward, not on this first manual bring-up.
Generate real MinIO credentials — never reuse
the `minioadmin`/`minioadmin` dev defaults in production:

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

Just push to `main`. No manual step needed.

To force a redeploy without a code change (e.g. after editing the server's
`.env`), SSH in and recreate the bot:

```bash
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose up -d bot'
```

## Changing app configuration (e.g. the Telegram allowlist)

App config lives only in the server's `.env` — editing a GitHub *secret* with the
same name (`TELEGRAM_ALLOWED_USERNAMES`, `ANTHROPIC_API_KEY`, etc.) does nothing,
since the workflow never reads those. To change a value:

```bash
ssh <user>@<server> "sed -i 's/^TELEGRAM_ALLOWED_USERNAMES=.*/TELEGRAM_ALLOWED_USERNAMES=alice,bob/' /opt/homelab/projects/pdf-to-anki/.env"
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose up -d bot'
```

Same pattern for the cleanup model, e.g. switching to Haiku 4.5:

```bash
ssh <user>@<server> "sed -i 's/^CLAUDE_MODEL=.*/CLAUDE_MODEL=claude-haiku-4-5-20251001/' /opt/homelab/projects/pdf-to-anki/.env"
ssh <user>@<server> 'cd /opt/homelab/projects/pdf-to-anki && sudo docker compose pull && sudo docker compose up -d'
```

`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are the exception to this whole section:
don't `sed` them on the server directly, since the next deploy overwrites both
from the GitHub secrets of the same name (see step 4 above). Change the GitHub
secret instead, then push (or re-run the deploy workflow) to roll it out.

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

- **Workflow's deploy job fails to connect.** Check `SERVER_HOST`/`SERVER_USER`
  are correct and the deploy key is still in the server's `authorized_keys`. If
  the job instead times out (`dial tcp ...:22: i/o timeout`) rather than being
  rejected, the server isn't reachable from the public internet on port 22 (e.g.
  a homelab box with SSH only exposed on the LAN) — the secrets can be entirely
  correct and it'll still fail. In that case, deploy manually from a machine
  that *can* reach it (see the SSH commands throughout this doc: `docker compose
  pull && docker compose up -d`), and `git push` only for CI to build/publish
  the image, not to reach the server itself. The server is at `192.168.1.172`,
  user `deepthinker`, port 22.
- **`docker compose pull` fails on the server.** The GHCR PAT may have expired,
  or `docker login ghcr.io` on the server needs re-running with a fresh
  `GHCR_TOKEN`.
- **Bot container restarts in a loop.** `docker compose logs bot` — almost
  always a missing/invalid value in the server's `.env`.
- **Bot container fails to start, or logs show it can't reach the Telegram
  API.** Check `docker compose logs telegram-bot-api` for auth errors — usually
  a wrong or missing `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` in `.env`. Also note
  that container's first boot can take a few seconds to authenticate with
  Telegram before the bot can use it.
