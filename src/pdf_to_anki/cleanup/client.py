from __future__ import annotations

import json
import logging

import anthropic
from pydantic import ValidationError

from ..config import Settings
from ..html_images import dedupe_images, img_tag, referenced_images, strip_images
from ..models import CleanedCard, ExtractedCard
from .prompts import SYSTEM_PROMPT
from .schema import CleanedCardOut, CleanupResponse

logger = logging.getLogger(__name__)

MAX_TOKENS = 16000


def cleanup_cards(
    cards: list[ExtractedCard],
    settings: Settings,
    client: anthropic.Anthropic | None = None,
) -> list[CleanedCard]:
    if not cards:
        return []

    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    try:
        response = client.messages.parse(
            model=settings.claude_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_payload(cards)}],
            output_format=CleanupResponse,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise ValueError("response contained no parsed structured output")
    except anthropic.NotFoundError:
        logger.warning("Cleanup skipped: model %r not found.", settings.claude_model)
        return _passthrough(cards)
    except anthropic.RateLimitError:
        logger.warning("Cleanup skipped: rate limited.")
        return _passthrough(cards)
    except anthropic.APIStatusError as exc:
        logger.warning("Cleanup skipped: API returned %s.", exc.status_code)
        return _passthrough(cards)
    except anthropic.APIConnectionError:
        logger.warning("Cleanup skipped: could not reach the API.")
        return _passthrough(cards)
    except ValidationError as exc:
        logger.warning("Cleanup skipped: model output failed validation: %s", exc)
        return _passthrough(cards)
    except Exception as exc:  # cleanup is a quality pass, never a blocker
        logger.warning("Cleanup skipped: unexpected failure: %r", exc)
        return _passthrough(cards)

    return _rebuild(cards, parsed)


def _passthrough(cards: list[ExtractedCard]) -> list[CleanedCard]:
    return [
        CleanedCard(
            question_text=card.question_text,
            answer_html=card.answer_html,
            source_page=card.source_page,
            image_refs=[img.filename for img in card.images],
        )
        for card in sorted(cards, key=lambda c: c.order_index)
    ]


def _build_payload(cards: list[ExtractedCard]) -> str:
    payload = [
        {
            "order_index": card.order_index,
            "question_text": card.question_text,
            "answer_html": card.answer_html,
        }
        for card in sorted(cards, key=lambda c: c.order_index)
    ]
    return (
        "Clean up the following extracted flashcards and return them as structured "
        "output.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _rebuild(cards: list[ExtractedCard], parsed: CleanupResponse) -> list[CleanedCard]:
    by_index = {card.order_index: card for card in cards}
    groups: dict[int, list[CleanedCardOut]] = {}
    for out in parsed.cards:
        if out.order_index not in by_index:
            logger.warning("Ignoring output card with unknown order_index %d.", out.order_index)
            continue
        groups.setdefault(out.order_index, []).append(out)

    # Judged batch-wide: segmentation can leave a tag in one card while the
    # ExtractedImage sits on its neighbour, and neither card may be "corrected".
    known = {img.filename for card in cards for img in card.images}
    shown = {
        name
        for outs in groups.values()
        for out in outs
        for name in referenced_images(out.answer_html)
        if name in known
    }

    result: list[CleanedCard] = []
    for card in sorted(cards, key=lambda c: c.order_index):
        outs = groups.get(card.order_index)
        if not outs:
            logger.warning(
                "Model omitted card %d; keeping raw content.", card.order_index
            )
            result.extend(_passthrough([card]))
            continue
        result.extend(_merge(card, outs, known, shown))
    return result


def _merge(
    card: ExtractedCard,
    outs: list[CleanedCardOut],
    known: set[str],
    shown: set[str],
) -> list[CleanedCard]:
    htmls = _reconcile_images(card, outs, known, shown)
    return [
        CleanedCard(
            question_text=out.question_text,
            answer_html=html,
            source_page=card.source_page,
            # The HTML is the source of truth: a split routes each image to the
            # sub-card that actually shows it.
            image_refs=referenced_images(html),
        )
        for out, html in zip(outs, htmls)
    ]


def _reconcile_images(
    card: ExtractedCard,
    outs: list[CleanedCardOut],
    known: set[str],
    shown: set[str],
) -> list[str]:
    """Return each output card's HTML with its <img> tags restored to the input set."""
    htmls = [out.answer_html for out in outs]

    for pos, html in enumerate(htmls):
        invented = [name for name in referenced_images(html) if name not in known]
        if invented:
            logger.warning(
                "Card %d: removing invented image reference(s): %s.",
                card.order_index,
                ", ".join(invented),
            )
            htmls[pos] = strip_images(html, invented)

    # Within one card a repeated tag renders the same picture twice; across cards
    # it is legitimate, so this set is deliberately not shared between groups.
    seen: set[str] = set()
    for pos, html in enumerate(htmls):
        htmls[pos] = dedupe_images(html, seen)
        if htmls[pos] != html:
            logger.warning(
                "Card %d: removed duplicate <img> tag(s) from sub-card %d.",
                card.order_index,
                pos,
            )

    lost = [
        img.filename
        for img in card.images
        if img.filename not in seen and img.filename not in shown
    ]
    if lost:
        logger.warning(
            "Card %d: cleanup dropped image(s), reattaching to the first sub-card: %s.",
            card.order_index,
            ", ".join(lost),
        )
        htmls[0] += "".join(img_tag(name) for name in lost)
    return htmls
