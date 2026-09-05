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
| `CLAUDE_MODEL` | `claude-sonnet-5` | model used for cleanup |
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
