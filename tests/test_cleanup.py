from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest

from pdf_to_anki.cleanup import cleanup_cards
from pdf_to_anki.cleanup.client import _passthrough
from pdf_to_anki.cleanup.schema import CleanedCardOut, CleanupResponse
from pdf_to_anki.config import Settings
from pdf_to_anki.models import ExtractedCard, ExtractedImage

PNG = b"\x89PNG\r\n\x1a\nFAKEPIXELS"


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test-key-not-real")


def image(xref: int, page: int = 1) -> ExtractedImage:
    return ExtractedImage(
        xref=xref,
        page_number=page,
        bbox=(0.0, 0.0, 10.0, 10.0),
        image_bytes=PNG,
        ext="png",
    )


def card(order_index: int, *, images: list[ExtractedImage] | None = None) -> ExtractedCard:
    return ExtractedCard(
        order_index=order_index,
        question_text=f"Küsimus {order_index}?",
        answer_html=f"<p>Vastus {order_index}</p>",
        source_page=order_index + 1,
        images=images or [],
    )


def mock_client(parsed: CleanupResponse | None = None, error: Exception | None = None) -> MagicMock:
    client = MagicMock()
    if error is not None:
        client.messages.parse.side_effect = error
    else:
        client.messages.parse.return_value = SimpleNamespace(parsed_output=parsed)
    return client


def out(order_index: int, suffix: str = "", html: str | None = None) -> CleanedCardOut:
    return CleanedCardOut(
        order_index=order_index,
        question_text=f"Küsimus {order_index}{suffix}?",
        answer_html=html or f"<p>Vastus {order_index}{suffix}</p>",
    )


def test_one_to_one_mapping_keeps_order_and_reattaches_images(settings):
    cards = [card(0, images=[image(11)]), card(1), card(2, images=[image(22), image(23)])]
    client = mock_client(CleanupResponse(cards=[out(0), out(1), out(2)]))

    result = cleanup_cards(cards, settings, client=client)

    assert [c.question_text for c in result] == ["Küsimus 0?", "Küsimus 1?", "Küsimus 2?"]
    assert [c.source_page for c in result] == [1, 2, 3]
    assert result[0].image_refs == ["img_p1_x11.png"]
    assert result[1].image_refs == []
    assert result[2].image_refs == ["img_p1_x22.png", "img_p1_x23.png"]


def test_out_of_order_model_output_is_sorted_by_order_index(settings):
    cards = [card(0), card(1)]
    client = mock_client(CleanupResponse(cards=[out(1), out(0)]))

    result = cleanup_cards(cards, settings, client=client)

    assert [c.question_text for c in result] == ["Küsimus 0?", "Küsimus 1?"]


def test_split_without_indices_puts_all_images_on_first_subcard(settings):
    cards = [card(0, images=[image(11), image(12)]), card(1)]
    client = mock_client(CleanupResponse(cards=[out(0, "a"), out(0, "b"), out(1)]))

    result = cleanup_cards(cards, settings, client=client)

    assert [c.question_text for c in result] == ["Küsimus 0a?", "Küsimus 0b?", "Küsimus 1?"]
    assert result[0].image_refs == ["img_p1_x11.png", "img_p1_x12.png"]
    assert result[1].image_refs == []
    assert all(c.source_page == 1 for c in result[:2])


def test_split_with_no_images_in_the_html_reattaches_to_the_first_subcard(settings):
    cards = [card(0, images=[image(11), image(12)])]
    client = mock_client(CleanupResponse(cards=[out(0, "a"), out(0, "b")]))

    result = cleanup_cards(cards, settings, client=client)

    assert result[0].image_refs == ["img_p1_x11.png", "img_p1_x12.png"]
    assert result[1].image_refs == []


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        anthropic.RateLimitError(
            "slow down",
            response=httpx2.Response(
                429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            body=None,
        ),
        RuntimeError("something unexpected"),
    ],
)
def test_api_failure_falls_back_to_passthrough(settings, error):
    cards = [card(0, images=[image(11)]), card(1)]
    client = mock_client(error=error)

    result = cleanup_cards(cards, settings, client=client)

    assert result == _passthrough(cards)
    assert [c.question_text for c in result] == ["Küsimus 0?", "Küsimus 1?"]
    assert result[0].image_refs == ["img_p1_x11.png"]


def test_schema_validation_failure_falls_back_to_passthrough(settings):
    cards = [card(0), card(1)]
    with pytest.raises(Exception) as excinfo:
        CleanupResponse.model_validate({"cards": [{"order_index": 0, "question_text": ""}]})
    client = mock_client(error=excinfo.value)

    result = cleanup_cards(cards, settings, client=client)

    assert result == _passthrough(cards)


def test_missing_parsed_output_falls_back_to_passthrough(settings):
    cards = [card(0), card(1)]
    client = mock_client(parsed=None)

    result = cleanup_cards(cards, settings, client=client)

    assert result == _passthrough(cards)


def test_omitted_card_falls_back_to_raw_content(settings, caplog):
    cards = [card(0, images=[image(11)]), card(1, images=[image(22)])]
    client = mock_client(CleanupResponse(cards=[out(0, "!")]))

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert [c.question_text for c in result] == ["Küsimus 0!?", "Küsimus 1?"]
    assert result[1].answer_html == "<p>Vastus 1</p>"
    assert result[1].image_refs == ["img_p1_x22.png"]
    assert "omitted card 1" in caplog.text


def test_unknown_order_index_is_ignored(settings, caplog):
    cards = [card(0)]
    client = mock_client(CleanupResponse(cards=[out(0), out(99)]))

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert len(result) == 1
    assert "unknown order_index 99" in caplog.text


def test_empty_input_makes_no_api_call(settings):
    client = mock_client(CleanupResponse(cards=[]))
    assert cleanup_cards([], settings, client=client) == []
    client.messages.parse.assert_not_called()


def test_request_transmits_no_image_bytes(settings):
    cards = [inline_card(0, INLINE_A, [image(11)]), card(1, images=[image(22)])]
    client = mock_client(CleanupResponse(cards=[out(0, html=INLINE_A), out(1)]))

    cleanup_cards(cards, settings, client=client)

    kwargs = client.messages.parse.call_args.kwargs
    sent = json.dumps(kwargs, default=str)
    assert "FAKEPIXELS" not in sent
    assert "image_bytes" not in sent
    assert "xref" not in sent
    assert "bbox" not in sent

    content = kwargs["messages"][0]["content"]
    payload = json.loads(content[content.index("[") :])
    assert payload == [
        {"order_index": 0, "question_text": "Küsimus 0?", "answer_html": INLINE_A},
        {"order_index": 1, "question_text": "Küsimus 1?", "answer_html": "<p>Vastus 1</p>"},
    ]
    # Filenames DO travel, by design: the model can only preserve an <img> it can see.
    assert "img_p1_x11.png" in payload[0]["answer_html"]
    # Card 1's image is not inline, so its name has no reason to be sent.
    assert "img_p1_x22.png" not in sent

    assert kwargs["model"] == settings.claude_model
    assert kwargs["output_format"] is CleanupResponse


def test_no_client_constructed_when_one_is_injected(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not construct a real Anthropic client")

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    client = mock_client(CleanupResponse(cards=[out(0)]))
    assert len(cleanup_cards([card(0)], settings, client=client)) == 1


INLINE_A = '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p>'
INLINE_B = '<p>Teine</p><img src="img_p1_x12.png"><p>Lõpp</p>'


def inline_card(order_index: int, html: str, images: list[ExtractedImage]) -> ExtractedCard:
    return ExtractedCard(
        order_index=order_index,
        question_text=f"Küsimus {order_index}?",
        answer_html=html,
        source_page=order_index + 1,
        images=images,
    )


def test_inline_image_survives_clean_one_to_one_cleanup(settings):
    cards = [inline_card(0, INLINE_A, [image(11)])]
    client = mock_client(CleanupResponse(cards=[out(0, html=INLINE_A)]))

    result = cleanup_cards(cards, settings, client=client)

    assert result[0].answer_html == INLINE_A
    assert result[0].image_refs == ["img_p1_x11.png"]


def test_image_refs_come_from_the_html(settings):
    cards = [inline_card(0, INLINE_A, [image(11), image(12)])]
    client = mock_client(CleanupResponse(cards=[out(0, html=INLINE_A + INLINE_B)]))

    result = cleanup_cards(cards, settings, client=client)

    assert result[0].image_refs == ["img_p1_x11.png", "img_p1_x12.png"]


def test_split_keeps_each_inline_image_with_its_own_card(settings):
    cards = [inline_card(0, INLINE_A + INLINE_B, [image(11), image(12)])]
    client = mock_client(
        CleanupResponse(cards=[out(0, "a", html=INLINE_A), out(0, "b", html=INLINE_B)])
    )

    result = cleanup_cards(cards, settings, client=client)

    assert [c.answer_html for c in result] == [INLINE_A, INLINE_B]
    assert result[0].image_refs == ["img_p1_x11.png"]
    assert result[1].image_refs == ["img_p1_x12.png"]


def test_dropped_inline_image_is_restored_with_a_warning(settings, caplog):
    cards = [inline_card(0, INLINE_A, [image(11)])]
    client = mock_client(CleanupResponse(cards=[out(0, html="<p>Enne</p><p>Pärast</p>")]))

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert result[0].answer_html.endswith('<img src="img_p1_x11.png">')
    assert result[0].image_refs == ["img_p1_x11.png"]
    assert "dropped image" in caplog.text
    assert "img_p1_x11.png" in caplog.text


def test_dropped_image_in_a_split_goes_to_the_first_subcard(settings, caplog):
    cards = [inline_card(0, INLINE_A + INLINE_B, [image(11), image(12)])]
    client = mock_client(
        CleanupResponse(
            cards=[out(0, "a", html="<p>Enne</p>"), out(0, "b", html=INLINE_B)]
        )
    )

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert result[0].image_refs == ["img_p1_x11.png"]
    assert result[1].answer_html == INLINE_B
    assert result[1].image_refs == ["img_p1_x12.png"]
    assert "dropped image" in caplog.text


def test_invented_image_reference_is_stripped_with_a_warning(settings, caplog):
    cards = [inline_card(0, INLINE_A, [image(11)])]
    evil = INLINE_A + '<img src="evil.png">'
    client = mock_client(CleanupResponse(cards=[out(0, html=evil)]))

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert result[0].answer_html == INLINE_A
    assert result[0].image_refs == ["img_p1_x11.png"]
    assert "invented image" in caplog.text
    assert "evil.png" in caplog.text


def test_passthrough_leaves_inline_images_untouched():
    cards = [inline_card(0, INLINE_A, [image(11)])]

    result = _passthrough(cards)

    assert result[0].answer_html == INLINE_A
    assert result[0].image_refs == ["img_p1_x11.png"]


def test_duplicated_image_tag_is_collapsed_to_the_first_occurrence(settings, caplog):
    cards = [inline_card(0, INLINE_A, [image(11)])]
    doubled = '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p><img src="img_p1_x11.png">'
    client = mock_client(CleanupResponse(cards=[out(0, html=doubled)]))

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert result[0].answer_html == INLINE_A
    assert result[0].image_refs == ["img_p1_x11.png"]
    assert "duplicate" in caplog.text


def test_same_image_kept_by_both_halves_of_a_split_survives_once(settings, caplog):
    cards = [inline_card(0, INLINE_A, [image(11)])]
    client = mock_client(
        CleanupResponse(cards=[out(0, "a", html=INLINE_A), out(0, "b", html=INLINE_A)])
    )

    with caplog.at_level("WARNING"):
        result = cleanup_cards(cards, settings, client=client)

    assert result[0].image_refs == ["img_p1_x11.png"]
    assert result[1].image_refs == []
    assert "<img" not in result[1].answer_html
    assert "duplicate" in caplog.text


def test_image_owned_by_one_card_but_shown_by_its_neighbour_stays_put(settings, caplog):
    # Segmentation can leave the tag in one card while the ExtractedImage lands on
    # its neighbour. Judging each card alone used to strip it here and re-append it
    # at the end of the wrong card - the exact bug inline positioning fixes.
    owner = ExtractedCard(0, "Küsimus 0?", "<p>Enne</p>", 1, [image(11)])
    shower = ExtractedCard(1, "Küsimus 1?", INLINE_A, 2, [])
    client = mock_client(
        CleanupResponse(cards=[out(0, html="<p>Enne</p>"), out(1, html=INLINE_A)])
    )

    with caplog.at_level("WARNING"):
        result = cleanup_cards([owner, shower], settings, client=client)

    assert result[0].answer_html == "<p>Enne</p>"
    assert result[0].image_refs == []
    assert result[1].answer_html == INLINE_A
    assert result[1].image_refs == ["img_p1_x11.png"]
    assert "invented" not in caplog.text
    assert "dropped image" not in caplog.text


def test_the_same_image_may_appear_in_two_different_cards(settings):
    # Two notes showing one picture is fine; only a repeat inside one note is not.
    a = ExtractedCard(0, "Küsimus 0?", INLINE_A, 1, [image(11)])
    b = ExtractedCard(1, "Küsimus 1?", INLINE_A, 2, [image(11)])
    client = mock_client(
        CleanupResponse(cards=[out(0, html=INLINE_A), out(1, html=INLINE_A)])
    )

    result = cleanup_cards([a, b], settings, client=client)

    assert [c.image_refs for c in result] == [["img_p1_x11.png"], ["img_p1_x11.png"]]
