from __future__ import annotations

import json
import logging
import re
import string
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.memory._hashing import compute_input_hash
from core.memory.exceptions import SlotMisconfigured
from core.memory.models import ConfidenceStub, Event, Fact

if TYPE_CHECKING:
    from core.memory.interfaces import FactStore
    from core.memory.slots import SlotResolver

logger = logging.getLogger(__name__)

_EXTRACTOR_VERSION = "v1"
_PROMPT_VERSION = "v1"
_GENERATION_METHOD = "extract_facts_from_event"

def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(string.punctuation)
    return text


_EXTRACTION_PROMPT_TEMPLATE = """\
Extract atomic, factual claims from the following assistant conversation turn.
Return ONLY valid JSON with this exact schema:
{{"facts": [{{"text": "...", "subject": "...", "predicate": "..."}}]}}

Each fact should be a single, verifiable claim. Include subject and predicate \
fields when possible. If you cannot determine subject/predicate, provide just \
the text field.

Conversation:
User: {user_message}
Assistant: {assistant_response}

Facts:"""


class FactExtractor:
    def __init__(
        self,
        slot_resolver: SlotResolver,
        fact_store: FactStore,
        slot_name: str = "extract_small",
    ) -> None:
        resolution = slot_resolver.get(slot_name)
        self._resolution = resolution
        self._fact_store = fact_store
        self._slot_name = slot_name

    async def extract(self, event: Event) -> list[Fact]:
        if event.kind != "agent_turn":
            return []

        backend = self._resolution.backend
        if backend != "lmstudio":
            raise SlotMisconfigured(
                self._slot_name,
                f"unsupported extractor backend: {backend}",
            )

        from core.backends.lmstudio import LMStudioClient

        client = LMStudioClient(base_url=self._resolution.endpoint)

        user_message = event.body.get("user_message", "")
        assistant_response = event.body.get("assistant_response", "")
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            user_message=user_message,
            assistant_response=assistant_response,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise fact extractor. Output only valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = await client.chat(
                messages=messages,
                model=self._resolution.model,
            )
        except Exception as exc:
            logger.warning(
                "FactExtractor: LLM call failed for event %s: %s",
                event.event_id,
                exc,
            )
            return []

        raw_content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if not raw_content:
            logger.warning(
                "FactExtractor: empty LLM response for event %s",
                event.event_id,
            )
            return []

        facts_data = self._parse_facts(raw_content)
        if facts_data is None:
            return []

        return await self._dedup_and_build_facts(facts_data, event)

    def _parse_facts(self, raw_content: str) -> list[dict] | None:
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            # Tolerate chat-template wrapping: depending on the serving stack,
            # models often fence the JSON in ```json ... ``` or add a short
            # preamble. Slice the outermost {...} and retry before giving up.
            start = raw_content.find("{")
            end = raw_content.rfind("}")
            if start == -1 or end <= start:
                logger.warning(
                    "FactExtractor: malformed JSON from LLM (no object found); head: %r",
                    raw_content[:200],
                )
                return None
            try:
                parsed = json.loads(raw_content[start : end + 1])
            except json.JSONDecodeError:
                logger.warning(
                    "FactExtractor: malformed JSON from LLM after fence-strip; head: %r",
                    raw_content[:200],
                )
                return None

        if not isinstance(parsed, dict):
            logger.warning("FactExtractor: LLM output is not a JSON object")
            return None

        facts_list = parsed.get("facts")
        if not isinstance(facts_list, list):
            logger.warning(
                "FactExtractor: response missing 'facts' array, got keys: %s",
                list(parsed.keys()),
            )
            return None

        valid_items: list[dict[str, str]] = []
        for item in facts_list:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            valid_item: dict[str, str] = {"text": text.strip()}
            if "subject" in item and isinstance(item["subject"], str):
                valid_item["subject"] = item["subject"]
            if "predicate" in item and isinstance(item["predicate"], str):
                valid_item["predicate"] = item["predicate"]
            valid_items.append(valid_item)

        if not valid_items:
            logger.warning(
                "FactExtractor: no valid fact items after schema validation"
            )
            return None

        return valid_items

    async def _dedup_and_build_facts(
        self,
        facts_data: list[dict],
        event: Event,
    ) -> list[Fact]:
        inputs = {
            "event.body": event.body,
            "event.kind": event.kind,
            "extractor.version": _EXTRACTOR_VERSION,
            "prompt.version": _PROMPT_VERSION,
        }
        input_hash = compute_input_hash(_GENERATION_METHOD, inputs)

        existing = await self._fact_store.query(
            {"generation_method": _GENERATION_METHOD}
        )
        if any(f.input_hash == input_hash for f in existing):
            return []

        existing_texts: set[str] = set()
        for f in existing:
            fact_text = f.body.get("text", "")
            if fact_text:
                existing_texts.add(_normalize(fact_text))

        ts = datetime.now(timezone.utc).isoformat()
        new_facts: list[Fact] = []
        for item in facts_data:
            item_text = item.get("text", "")
            if _normalize(item_text) in existing_texts:
                logger.info(
                    "per_fact_dedup_skip event_id=%s text=%r",
                    event.event_id, item_text,
                )
                continue

            body = dict(item)
            fact = Fact(
                fact_id=uuid.uuid4().hex,
                generation_method=_GENERATION_METHOD,
                body=body,
                source_event_id=event.event_id,
                input_hash=input_hash,
                confidence=ConfidenceStub(),
                ts=ts,
            )
            new_facts.append(fact)

        return new_facts
