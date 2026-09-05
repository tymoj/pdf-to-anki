from __future__ import annotations

import json
import logging
import sqlite3
import zipfile
from pathlib import Path

import pytest

from pdf_to_anki.anki import MODEL_ID, write_apkg
from pdf_to_anki.models import CleanedCard

PNG_HEADER = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 16
FIELD_SEP = "\x1f"


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    d = tmp_path / "media"
    d.mkdir()
    (d / "img_p3_x42.png").write_bytes(PNG_HEADER)
    return d


@pytest.fixture
def cards() -> list[CleanedCard]:
    return [
        CleanedCard(
            question_text="Millised on aneemia põhjused?",
            answer_html="<ul><li><b>Rauapuudus</b></li><li>Verekaotus</li></ul>",
            source_page=3,
        ),
        CleanedCard(
            question_text="Mis on lümfotsüüt?",
            answer_html="<p>Valgeliblede alatüüp.</p>",
            source_page=5,
        ),
        CleanedCard(
            question_text="Kirjelda erütrotsüütide morfoloogiat",
            answer_html='<p>Vaata pilti:</p><img src="img_p3_x42.png">',
            source_page=3,
            image_refs=["img_p3_x42.png"],
        ),
    ]


def read_collection(apkg: Path, tmp_path: Path) -> sqlite3.Connection:
    with zipfile.ZipFile(apkg) as zf:
        names = zf.namelist()
        assert "collection.anki2" in names, names
        db_path = tmp_path / "extracted.anki2"
        db_path.write_bytes(zf.read("collection.anki2"))
    return sqlite3.connect(db_path)


def test_writes_valid_apkg(cards, media_dir, tmp_path):
    out = tmp_path / "out" / "deck.apkg"
    result = write_apkg(cards, "Hematopoees", media_dir, out)

    assert result == out
    assert out.exists()
    assert out.stat().st_size > 1000

    conn = read_collection(out, tmp_path)
    try:
        assert conn.execute("select count(*) from notes").fetchone()[0] == len(cards)
        assert conn.execute("select count(*) from cards").fetchone()[0] == len(cards)

        flds = [row[0] for row in conn.execute("select flds from notes")]
        joined = "\n".join(flds)
        assert "aneemia põhjused" in joined
        assert "lümfotsüüt" in joined
        assert "<ul><li><b>Rauapuudus</b></li>" in joined
        assert all(FIELD_SEP in f for f in flds)

        models = json.loads(conn.execute("select models from col").fetchone()[0])
        assert str(MODEL_ID) in models
        assert [f["name"] for f in models[str(MODEL_ID)]["flds"]] == ["Question", "Answer"]
    finally:
        conn.close()


def test_media_is_packaged(cards, media_dir, tmp_path):
    out = tmp_path / "deck.apkg"
    write_apkg(cards, "Hematopoees", media_dir, out)

    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("media"))
        assert set(manifest.values()) == {"img_p3_x42.png"}
        for idx in manifest:
            assert zf.read(idx) == PNG_HEADER


def test_no_media_dir_is_fine(tmp_path):
    cards = [CleanedCard("Küsimus?", "<p>Vastus</p>", 1)]
    out = tmp_path / "deck.apkg"
    write_apkg(cards, "Hematopoees", None, out)

    with zipfile.ZipFile(out) as zf:
        assert json.loads(zf.read("media")) == {}
    conn = read_collection(out, tmp_path)
    try:
        assert conn.execute("select count(*) from notes").fetchone()[0] == 1
    finally:
        conn.close()


def test_duplicate_image_refs_are_deduplicated(media_dir, tmp_path):
    cards = [
        CleanedCard("Osa 1?", "<img src='img_p3_x42.png'>", 3, ["img_p3_x42.png"]),
        CleanedCard("Osa 2?", "<img src='img_p3_x42.png'>", 3, ["img_p3_x42.png"]),
    ]
    out = tmp_path / "deck.apkg"
    write_apkg(cards, "Hematopoees", media_dir, out)

    with zipfile.ZipFile(out) as zf:
        assert len(json.loads(zf.read("media"))) == 1


def test_deterministic_deck_id_and_guids(cards, media_dir, tmp_path):
    def build(tag: str) -> tuple[int, list[str]]:
        work = tmp_path / tag
        work.mkdir()
        out = work / "deck.apkg"
        write_apkg(cards, "Hematopoees", media_dir, out)
        conn = read_collection(out, work)
        try:
            decks = json.loads(conn.execute("select decks from col").fetchone()[0])
            deck_id = max(int(k) for k in decks)
            guids = sorted(row[0] for row in conn.execute("select guid from notes"))
            return deck_id, guids
        finally:
            conn.close()

    first_id, first_guids = build("first")
    second_id, second_guids = build("second")

    assert first_id == second_id
    assert first_guids == second_guids
    assert len(set(first_guids)) == len(cards)


def test_missing_media_warns_but_still_writes(media_dir, tmp_path, caplog):
    cards = [CleanedCard("Kus on pilt?", "<p>x</p>", 7, ["img_p7_x99.png"])]
    out = tmp_path / "deck.apkg"

    with caplog.at_level(logging.WARNING, logger="pdf_to_anki.anki.writer"):
        write_apkg(cards, "Hematopoees", media_dir, out)

    assert any("img_p7_x99.png" in r.getMessage() for r in caplog.records)
    assert out.exists()
    conn = read_collection(out, tmp_path)
    try:
        assert conn.execute("select count(*) from notes").fetchone()[0] == 1
    finally:
        conn.close()
    with zipfile.ZipFile(out) as zf:
        assert json.loads(zf.read("media")) == {}
