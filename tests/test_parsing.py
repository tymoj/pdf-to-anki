from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest

from pdf_to_anki.parsing import extract_cards, is_question_color, rgb_from_span_color
from pdf_to_anki.parsing.html import (
    Element,
    ImageElement,
    StyledRun,
    render_elements,
    render_runs,
)
from pdf_to_anki.parsing.segment import _is_continuation, _Line, _line_runs

ALLOWED_TAGS = {"p", "br", "b", "i", "ul", "li", "img"}

# The deck has ten green (rgb 80,148,110) size-18 question runs; the last two
# both live on page 8.
EXPECTED_QUESTIONS = [
    "Palun kirjeldage vererakkude teket.",
    "Palun nimetage vererakkude liinid ja nende küpsemise paikmed.",
    "Mis on hematomedullaarne barjäär? Nimetage hematomedullaarse barjääri funktsioonid.",
    "Palun kirjeldage lümfotsüütide peamiseid tüüpe ja nende markereid.",
    "Palun nimetage B-sümptomid.",
    "Millised on peamised uurimismeetodid pahaloomuliste hematoloogiliste haiguste "
    "diagnoosimisel?",
    "Palun nimetage neutrofiilia peamised põhjused.",
    "Palun nimetage lümfotsütoosi peamised põhjused.",
    "Palun nimetage eosinofiilia peamised põhjused.",
    "Palun nimetage trombotsütoosi peamised põhjused.",
]


@pytest.fixture(scope="module")
def cards(sample_pdf):
    with pymupdf.open(sample_pdf) as doc:
        return extract_cards(doc)


@pytest.fixture(scope="module")
def image_pdf() -> Path:
    path = Path(__file__).resolve().parents[1] / "pdf" / "5.praktikum.pdf"
    if not path.exists():
        pytest.skip(f"sample PDF missing: {path}")
    return path


@pytest.fixture(scope="module")
def image_cards(image_pdf):
    with pymupdf.open(image_pdf) as doc:
        return extract_cards(doc)


def _card_by_question(cards, needle: str):
    return next(c for c in cards if needle in c.question_text)


def test_extracts_every_question(cards):
    assert [c.question_text for c in cards] == EXPECTED_QUESTIONS


def test_question_text_is_space_joined(cards):
    assert cards[0].question_text == "Palun kirjeldage vererakkude teket."


def test_order_index_is_sequential(cards):
    assert [c.order_index for c in cards] == list(range(len(cards)))


def test_every_card_has_an_answer(cards):
    for card in cards:
        assert card.answer_html.strip()


def test_answer_html_uses_only_whitelisted_tags(cards):
    for card in cards:
        found = set(re.findall(r"</?([a-zA-Z0-9]+)", card.answer_html))
        assert found <= ALLOWED_TAGS, f"card {card.order_index} emitted {found - ALLOWED_TAGS}"


def test_answers_contain_lists_and_bold(cards):
    assert any("<ul>" in c.answer_html and "<li>" in c.answer_html for c in cards)
    assert any("<b>" in c.answer_html for c in cards)


def test_answers_contain_nested_lists(cards):
    assert any("<li><b>sekundaarne ehk reaktiivne trombotsütoos</b><ul>" in c.answer_html
               for c in cards)


def test_source_pages_are_ordered_and_in_range(cards):
    pages = [c.source_page for c in cards]
    assert all(1 <= p <= 9 for p in pages)
    assert pages == sorted(pages)


def test_estonian_characters_survive(cards):
    assert "lümfoidne rakuliin" in cards[0].answer_html
    assert "küpsemine" in cards[1].answer_html
    assert "barjäär" in cards[2].question_text


def test_answer_continues_across_a_page_break(cards):
    # The sentence starts at the bottom of page 3 and finishes on page 4.
    assert "umbes <b>70% T-rakud, 15% B-rakud" in cards[3].answer_html


def test_html_special_characters_are_escaped(cards):
    assert "&gt;" in cards[6].answer_html
    assert "<b>Neut &gt;" in cards[6].answer_html


def test_question_text_escapes_html_special_characters(sample_pdf, monkeypatch):
    # Neither deck contains one naturally, but a marker named in angle brackets
    # ("T<CD4> ja <CD8> rakud?") is dropped outright by the Anki HTML renderer.
    from pdf_to_anki.parsing import segment

    monkeypatch.setattr(segment, "_joined_text", lambda lines: "T<CD4> & <CD8> rakud?")
    with pymupdf.open(sample_pdf) as doc:
        card = extract_cards(doc)[0]
    assert card.question_text == "T&lt;CD4&gt; &amp; &lt;CD8&gt; rakud?"
    assert "<" not in card.question_text and ">" not in card.question_text


def test_question_text_carries_no_markup(cards, image_cards):
    for card in list(cards) + list(image_cards):
        assert not re.search(r"</?[a-zA-Z]", card.question_text)


def test_image_lands_where_the_pdf_places_it(image_cards):
    # Page 2 reads: prose, "the most important differences are:", the comparison
    # table, then more prose. The table has to sit between the two.
    card = _card_by_question(image_cards, "lümfoomi erinevused")
    html = card.answer_html
    assert html.index("Kõige olulisemad erinevused on järgmised:") < html.index(
        '<img src="img_p2_x23.png">'
    ) < html.index("Kõige tähtsam morfoloogiline erinevus")
    # The line after the image starts a fresh element rather than continuing one.
    assert '<img src="img_p2_x23.png"><p>Kõige tähtsam' in html


def test_image_above_the_first_text_of_a_continuation_page(image_cards):
    # The page-8 image sits at y72 while that page's only text starts at y237, so
    # it belongs to the answer continuing from page 7 and precedes that text.
    card = _card_by_question(image_cards, "Kuidas jaotatakse üldiselt mitte-Hodgkin")
    assert card.source_page == 7
    assert [image.filename for image in card.images] == ["img_p8_x54.png"]
    html = card.answer_html
    assert html.index("Eriti B-rakuliste NHL-ide puhul eristatakse") < html.index(
        '<img src="img_p8_x54.png">'
    ) < html.index("Meelespea")


def test_every_image_in_the_document_is_referenced_once(image_cards):
    refs = [
        ref
        for card in image_cards
        for ref in re.findall(r'<img src="([^"]+)">', card.answer_html)
    ]
    assert sorted(refs) == [
        "img_p11_x69.png",
        "img_p2_x23.png",
        "img_p6_x45.png",
        "img_p8_x54.png",
    ]


def test_card_images_match_their_html_references(image_cards):
    for card in image_cards:
        refs = re.findall(r'<img src="([^"]+)">', card.answer_html)
        assert refs == [image.filename for image in card.images]


def test_every_referenced_image_is_owned_by_its_card(cards, image_cards):
    # Cleanup routes images by this invariant. If a continuation rule ever moves
    # an element across a card boundary without its ExtractedImage, cleanup drops
    # the reference and reattaches the file to the wrong card at the very end.
    for card in list(cards) + list(image_cards):
        refs = set(re.findall(r'<img src="([^"]+)">', card.answer_html))
        assert refs <= {image.filename for image in card.images}


def test_images_are_block_level(image_cards):
    for card in image_cards:
        assert "<p><img" not in card.answer_html
        assert "<li><img" not in card.answer_html


def test_image_answers_use_only_whitelisted_tags(image_cards):
    for card in image_cards:
        found = set(re.findall(r"</?([a-zA-Z0-9]+)", card.answer_html))
        assert found <= ALLOWED_TAGS, f"card {card.order_index} emitted {found - ALLOWED_TAGS}"


def test_render_elements_emits_images_between_blocks():
    html = render_elements(
        [
            Element(0, [StyledRun("before")]),
            ImageElement("img_p1_x2.png"),
            Element(0, [StyledRun("after")]),
        ]
    )
    assert html == '<p>before</p><img src="img_p1_x2.png"><p>after</p>'


def test_render_elements_closes_lists_before_an_image():
    html = render_elements(
        [
            Element(1, [StyledRun("item")]),
            ImageElement("i.png"),
            Element(1, [StyledRun("next")]),
        ]
    )
    assert html == '<ul><li>item</li></ul><img src="i.png"><ul><li>next</li></ul>'


def test_a_heading_does_not_glue_onto_the_paragraph_below_it(cards):
    # These 15pt headings sit 25.49933pt above the 12pt line under them while the
    # wrap threshold is 25.49934: only the size gate separates them reliably.
    html = cards[0].answer_html
    assert "<p><b>Müeloidne vereloome</b></p><p><b>Granulopoees:</b></p>" in html
    assert "<p><b>Lümfopoees</b></p><p>B-rakud küpsevad esmalt" in html


# The six lines in 5.praktikum that indent like a list item but carry no bullet
# glyph: each is the tail of a wrapped line and belongs to the text above it.
WRAPPED_TAILS = [
    "üks lümfisõlmede regioon või üks lümfoidne struktuur, näiteks põrn,",
    "regiooni, kuid kõik asuvad samal pool diafragmat.",
    "on haaratud mõlemal pool diafragmat.",
    "haigus on levinud väljapoole lümfisüsteemi organitesse/kudedesse.",
    "umbes <b>3,7% NHL-idest</b>.",
    "umbes <b>2,4% NHL-idest</b>.",
]

SPURIOUS_BULLETS = [
    "<li>struktuur, näiteks",
    "<li>asuvad samal",
    "<li>pool diafragmat",
    "<li>levinud väljapoole",
    "<li><b>3,7% NHL-idest",
    "<li><b>idest",
]


def test_unbulleted_wrapped_lines_stay_with_their_paragraph(image_cards):
    html = "".join(card.answer_html for card in image_cards)
    for tail in WRAPPED_TAILS:
        assert tail in html
    for bullet in SPURIOUS_BULLETS:
        assert bullet not in html


def test_sub_items_of_a_skipped_level_stay_siblings(image_cards):
    card = _card_by_question(image_cards, "Ann Arbor")
    assert (
        "<ul><li>III₁ – haaratud võivad olla põrn, hiiluse, tsöliaakia- või "
        "portaalsed lümfisõlmed.</li><li>III₂ – haaratud on paraaortaalsed"
    ) in card.answer_html


def test_unfinished_sentences_rejoin_across_a_page_break(image_cards):
    raviskeemid = _card_by_question(image_cards, "peamised raviskeemid")
    assert (
        "haiguse staadiumist ja riskist. Õppejõu slaidides on põhirõhk"
        in raviskeemid.answer_html
    )
    kll = _card_by_question(image_cards, "KLL raviprintsiibid")
    assert (
        "patsientidel</b>, näiteks refraktaarse haiguse või varajase retsidiivi"
        in kll.answer_html
    )


def _bullet_glyphs(path) -> int:
    with pymupdf.open(path) as doc:
        return sum(
            1
            for page in doc
            for drawing in page.get_drawings()
            if 3.0 <= drawing["rect"].width <= 5.0
            and 3.0 <= drawing["rect"].height <= 5.0
            and abs(drawing["rect"].width - drawing["rect"].height) <= 1.0
        )


def test_every_list_item_traces_to_a_drawn_bullet(cards, sample_pdf, image_cards, image_pdf):
    # Three glyphs in each deck sit above the first question and are dropped
    # along with that text; every remaining one becomes exactly one <li>.
    assert _bullet_glyphs(sample_pdf) - 3 == sum(
        c.answer_html.count("<li>") for c in cards
    ) == 97
    assert _bullet_glyphs(image_pdf) - 3 == sum(
        c.answer_html.count("<li>") for c in image_cards
    ) == 32


def _mk_line(
    text: str,
    *,
    page: int = 1,
    y0: float = 100.0,
    size: float = 12.0,
    level: int = 0,
    has_bullet: bool = False,
    x1: float = 400.0,
    page_max_x1: float = 500.0,
) -> _Line:
    return _Line(
        page_number=page,
        x0=72.0,
        x1=x1,
        y0=y0,
        y1=y0 + size,
        size=size,
        is_question=False,
        runs=[StyledRun(text)],
        page_max_x1=page_max_x1,
        level=level,
        has_bullet=has_bullet,
    )


def test_size_gate_separates_a_heading_from_body_text():
    heading = _mk_line("Lümfopoees", y0=147.7, size=15.0)
    assert not _is_continuation(heading, _mk_line("B-rakud küpsevad", y0=173.2))


def test_bullet_gate_distinguishes_a_wrap_from_a_list_item():
    prev = _mk_line("kaks või enam regiooni, kuid kõik", y0=447.8)
    assert _is_continuation(prev, _mk_line("asuvad samal pool", y0=465.8, level=1))
    assert not _is_continuation(
        prev, _mk_line("asuvad samal pool", y0=465.8, level=1, has_bullet=True)
    )


def test_bullet_gate_does_not_let_a_shallower_line_continue():
    prev = _mk_line("deep item", level=1)
    assert not _is_continuation(prev, _mk_line("back out", y0=118.0, level=0))


def test_page_break_joins_only_unfinished_lowercase_prose():
    # x1 is far from the margin, so the wrap test alone would reject all of these.
    prev = _mk_line("staadiumist ja riskist. Õppejõu")
    assert _is_continuation(prev, _mk_line("slaidides on põhirõhk", page=2, y0=74.3))
    assert not _is_continuation(prev, _mk_line("Slaidides on põhirõhk", page=2, y0=74.3))
    finished = _mk_line("lause on lõpetatud.")
    assert not _is_continuation(finished, _mk_line("järgmine lause", page=2, y0=74.3))


def test_page_break_rule_treats_a_trailing_comma_as_unfinished():
    prev = _mk_line("noortel kõrge riskiga patsientidel,")
    assert _is_continuation(prev, _mk_line("näiteks refraktaarse", page=2, y0=74.3))


def test_page_break_rule_never_chains_list_items():
    # Estonian list items are lowercase and unpunctuated, so the linguistic rule
    # would merge consecutive bullets if it were not restricted to level 0.
    prev = _mk_line("hemogramm koos 5-osalise leukogrammiga", level=1)
    nxt = _mk_line("retikulotsüüdid", page=2, y0=74.3, level=1, has_bullet=True)
    assert not _is_continuation(prev, nxt)


def test_is_question_color_tolerance():
    assert is_question_color((80, 148, 110))
    assert is_question_color((100, 130, 130), tolerance=30)
    assert not is_question_color((111, 148, 110), tolerance=30)
    assert not is_question_color((0, 0, 0))
    assert is_question_color((111, 148, 110), tolerance=31)


def test_rgb_from_span_color():
    assert rgb_from_span_color(0x50946E) == (80, 148, 110)
    assert rgb_from_span_color(0) == (0, 0, 0)


def _span(text: str, x0: float, x1: float, font: str = "Type3 (7 0 R)") -> dict:
    return {"text": text, "bbox": (x0, 0.0, x1, 12.0), "font": font}


def test_line_runs_space_joining():
    spans = [
        _span("Palun", 72.0, 119.8),
        _span("kirjeldage", 124.0, 208.3),
        _span("vererakkude", 212.5, 319.6),
        _span("teket", 323.8, 366.8),
        _span(".", 366.6, 372.3),
    ]
    runs = _line_runs(spans, lambda font: (False, False))
    assert render_runs(runs) == "Palun kirjeldage vererakkude teket."


def test_line_runs_keeps_span_trailing_space():
    spans = [_span("protsess", 299.9, 348.6), _span(", ", 348.6, 355.4), _span("mille", 355.4, 381.6)]
    assert render_runs(_line_runs(spans, lambda font: (False, False))) == "protsess, mille"


def test_adjacent_identical_styles_merge_into_one_tag():
    runs = [StyledRun("äge", bold=True), StyledRun(" "), StyledRun("põletik", bold=True)]
    assert render_runs(runs) == "<b>äge põletik</b>"


def test_mixed_styles_stay_separate():
    runs = [StyledRun("a", bold=True), StyledRun(" "), StyledRun("b")]
    assert render_runs(runs) == "<b>a</b> b"


def test_render_runs_escapes_markup():
    assert render_runs([StyledRun("a < b & c")]) == "a &lt; b &amp; c"


def test_render_elements_nests_and_closes_lists():
    html = render_elements(
        [
            Element(0, [StyledRun("intro")]),
            Element(1, [StyledRun("one")]),
            Element(2, [StyledRun("deep")]),
            Element(1, [StyledRun("two")]),
            Element(0, [StyledRun("outro")]),
        ]
    )
    assert html == (
        "<p>intro</p><ul><li>one<ul><li>deep</li></ul></li><li>two</li></ul><p>outro</p>"
    )


def test_render_elements_keeps_siblings_when_a_level_is_skipped():
    html = render_elements([Element(2, [StyledRun("one")]), Element(2, [StyledRun("two")])])
    assert html == "<ul><li>one</li><li>two</li></ul>"


def test_render_elements_drops_empty_elements():
    assert render_elements([Element(0, [StyledRun("  ")]), Element(0, [StyledRun("x")])]) == "<p>x</p>"
