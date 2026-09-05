from __future__ import annotations

import genanki

# Hardcoded so re-imports merge into the same note type instead of creating a duplicate.
MODEL_ID = 1739205461

CSS = """
.card {
  font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 19px;
  line-height: 1.5;
  text-align: left;
  color: #1a1a1a;
  background-color: #fff;
}
.card ul, .card ol { margin: 0.4em 0; padding-left: 1.4em; }
.card li { margin: 0.2em 0; }
img { max-width: 100%; height: auto; }
hr#answer { margin: 0.8em 0; border: none; border-top: 1px solid #bbb; }
"""

MODEL = genanki.Model(
    MODEL_ID,
    "PDF to Anki Q&A",
    fields=[{"name": "Question"}, {"name": "Answer"}],
    templates=[
        {
            "name": "Q&A",
            "qfmt": "{{Question}}",
            "afmt": '{{FrontSide}}<hr id=answer>{{Answer}}',
        }
    ],
    css=CSS,
)
