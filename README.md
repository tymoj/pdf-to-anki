# pdf-to-anki

Turns a colour-marked PDF of study material into an Anki `.apkg` deck.

Cards come from the document's own markup rather than being invented: **text in the
marker colour (green) is the question**, and everything until the next green run is its
answer, with the original bold/list/paragraph formatting preserved as HTML. The Claude
API is used only to clean up PDF-extraction artifacts — never to add or alter facts.

## Setup

```bash
uv sync
cp .env.example .env       # then fill in ANTHROPIC_API_KEY
```

## Usage

```bash
uv run pdf-to-anki "pdf/1.praktikum hematopoees.pdf" -o deck.apkg
```

Import the resulting `.apkg` into Anki. Re-running on the same PDF updates the same deck
and the same notes rather than creating duplicates (deck ID is derived from the deck
name; note GUIDs from the question text and page).

## Library use

`pdf_to_anki()` is the single entry point, and takes a path *or* raw bytes — so a
Telegram bot handler can pass an uploaded file straight through:

```python
from pdf_to_anki import pdf_to_anki

apkg_path = pdf_to_anki(uploaded_bytes)      # returns a Path to the .apkg
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(required)* | Claude API key |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | model used for cleanup |
| `QUESTION_MARKER_RGB` | `80,148,110` | the colour that marks questions |
| `QUESTION_MARKER_TOLERANCE` | `30` | per-channel RGB match tolerance |

The marker colour is document-specific. To calibrate it for a new PDF:

```bash
uv run python scripts/inspect_spans.py path/to.pdf
```

which dumps every text span's colour, size, font and position, ending with a histogram
of colours by character count — the marker colour is normally the distinctive
non-black entry.

If the Claude cleanup call fails for any reason, the deck is still built from the raw
extracted text; cleanup is a quality pass, not a dependency.

## Tests

```bash
uv run pytest
```

All tests are network-free and need no API key.

## Deployment

The Telegram bot, an async worker, Redis and MinIO run as one Docker Compose stack.
The bot only accepts uploads and enqueues them; the worker does every PDF parse,
Claude call and upload, and sends the finished deck back itself.

### Prerequisites

- Docker with Compose v2 (`docker compose version`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An Anthropic API key

### Configure

```bash
cp .env.example .env
```

Fill in:

| Variable | Notes |
| --- | --- |
| `ANTHROPIC_API_KEY` | required |
| `TELEGRAM_BOT_TOKEN` | required — the worker needs it too, since it sends the deck |
| `TELEGRAM_ALLOWED_USERNAMES` | required; the bot refuses to start with an empty allowlist |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | become MinIO's root credentials — change them from `minioadmin` |

`REDIS_URL` and `S3_ENDPOINT_URL` are overridden in `docker-compose.yml` with the
compose-internal hostnames, so whatever `.env` says for those is ignored inside the
stack. Two optional extras:

| Variable | Default | Meaning |
| --- | --- | --- |
| `S3_PUBLIC_ENDPOINT_URL` | `http://localhost:9000` | address a Telegram user can reach MinIO on; pre-signed links are signed against it |
| `LOG_LEVEL` | `INFO` | log level for bot and worker |

Secrets live only in `.env`, which is git-ignored and excluded from the image.
Note that `docker compose config` prints the interpolated values, tokens included —
don't paste its output anywhere.

### Run

```bash
docker compose up -d --build
docker compose logs -f bot worker
```

The worker is the service that does the work, so it is the one to scale:

```bash
docker compose up -d --scale worker=3
```

Bot and worker share one image; only the entry point differs.

### MinIO

The console is at <http://localhost:9001>, signed in with `S3_ACCESS_KEY` /
`S3_SECRET_KEY` (`minioadmin` / `minioadmin` until you change them). Both the source
PDFs and the generated decks are kept, under `pdfs/{user_id}/{job_id}.pdf` and
`decks/{user_id}/{job_id}.apkg`. A one-shot `minio-init` service creates the bucket
before the bot or worker starts.

### Troubleshooting

- **The bot ignores you.** Check your handle is in `TELEGRAM_ALLOWED_USERNAMES`
  (case-insensitive, `@` optional) and then `docker compose logs bot` — an
  unauthorised chat is logged and dropped. Only one process may poll a given token,
  so a second bot running elsewhere on the same token will starve this one.
- **Uploads are accepted but no deck ever comes back.** That is the worker or the
  queue: `docker compose logs worker` and `docker compose ps redis`. A job that fails
  transiently is retried up to 3 times and the user is only told after the last one,
  so give it a minute before concluding it is stuck.
- **"Storage is unavailable"**, or the worker dies at startup. `docker compose ps
  minio` should show `healthy`, and `docker compose logs minio-init` should end with
  `bucket ready`. `docker compose restart minio-init` recreates the bucket.
- **A deck arrives as a link instead of a file.** It was over Telegram's 50 MB limit
  for bot uploads. If the link does not open, `S3_PUBLIC_ENDPOINT_URL` is pointing
  somewhere the recipient cannot reach.
- **"No question-marked text found".** The marker colour does not match that PDF; see
  `QUESTION_MARKER_RGB` above. This is a permanent failure and is not retried.

### Continuous deployment

Every push to `main` triggers `.github/workflows/deploy.yml`:

1. Builds a multi-arch (amd64/arm64) image and pushes it to
   `ghcr.io/tymoj/pdf-to-anki:latest`
2. SSHes into the server, `docker compose pull`s the new image, and
   `docker compose up -d`s the stack

Required GitHub repo secrets: `SERVER_HOST`, `SERVER_USER`, `SSH_PRIVATE_KEY`,
`GHCR_TOKEN` (a PAT with `read:packages`/`write:packages`, used by the server to
`docker login ghcr.io`).

This expects the server already has Docker and the project directory in place —
first-time setup is manual, not scripted:

```bash
mkdir -p /opt/homelab/projects/pdf-to-anki
cd /opt/homelab/projects/pdf-to-anki
# copy docker-compose.yml here, then create .env (see Configure above)
sudo docker compose pull && sudo docker compose up -d
```

No public HTTP endpoint is needed — the bot only makes outbound connections to
Telegram and Claude — so nothing here touches Traefik.
