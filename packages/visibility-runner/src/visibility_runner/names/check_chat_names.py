"""Parse, check and save names listed in a measurement conversation.

The name list must be one JSON array. Names are matched exactly against the
first response, preserving order and duplicates. Each turn has its own saved
send attempt; an uncertain send is never repeated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


MENTION_EXTRACTION_SCHEMA_VERSION = "free_snapshot.same_chat_name_extraction.v1"
MENTION_EXTRACTION_PROMPT_VERSION = "same-chat-name-extraction-v3"
MENTION_EXTRACTION_PROMPT = (
    "Return only the clinic/provider names you mentioned in your immediately "
    "preceding answer, in the same order. Copy each name as one uninterrupted "
    "verbatim text span from that answer, preserving spelling, accents, "
    "punctuation and spacing. Do not join a clinic name with an adjacent "
    "citation, source label, link title or other text. Preserve repeated name "
    "occurrences as separate entries. Do not add explanations, classifications, "
    "offsets, or names you did "
    "not mention. Return a closed JSON array of strings; return [] if none. "
    "Do not wrap the array in Markdown or a code fence."
)
CONVERSATIONAL_PLATFORMS = frozenset({"chatgpt", "gemini", "copilot"})
NON_CONVERSATIONAL_PLATFORMS = frozenset({"google_ai_overview"})


class MentionExtractionError(ValueError):
    """A same-chat name listing artifact or response failed closed."""


class AmbiguousTurnError(MentionExtractionError):
    """A durable send attempt exists without a durable acknowledgement."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value)
    if cleaned == value and cleaned and len(cleaned) <= 120:
        return cleaned
    prefix = (cleaned[:90] or "observation").rstrip("_")
    return f"{prefix}-{_sha256_text(value)[:16]}"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as target:
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _read_text_losslessly(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as source:
        return source.read()


def parse_closed_name_array(response: object) -> list[str]:
    """Parse exactly one closed JSON array containing only non-empty strings.

    Leading and trailing JSON whitespace are accepted. Markdown send attempts, prose,
    objects, extra keys/tokens, non-strings, malformed JSON, and empty names are
    rejected.
    """

    if not isinstance(response, str):
        raise MentionExtractionError("name extraction response must be JSON text")
    json_whitespace = " \t\r\n"
    if not response.strip(json_whitespace):
        raise MentionExtractionError("name extraction response is empty")
    decoder = json.JSONDecoder()
    leading = len(response) - len(response.lstrip(json_whitespace))
    try:
        value, end = decoder.raw_decode(response, idx=leading)
    except json.JSONDecodeError as exc:
        raise MentionExtractionError("name extraction response is malformed JSON") from exc
    if response[end:].strip(json_whitespace):
        raise MentionExtractionError(
            "name extraction response contains content outside the JSON array"
        )
    if not isinstance(value, list):
        raise MentionExtractionError("name extraction response must be a JSON array")
    if any(not isinstance(name, str) for name in value):
        raise MentionExtractionError(
            "name extraction response array may contain only strings"
        )
    if any(not name.strip() for name in value):
        raise MentionExtractionError("name extraction response names cannot be empty")
    return list(value)


def find_name_positions(raw_answer: str, names: Sequence[str]) -> list[dict[str, Any]]:
    """Ground every supplied name at or after the preceding matched occurrence.

    Duplicate names are preserved. Each duplicate consumes the next distinct,
    non-overlapping exact occurrence. A missing next occurrence fails the whole
    name listing; names are never deduplicated, reordered, or case-normalized.
    """

    if not isinstance(raw_answer, str):
        raise MentionExtractionError("raw answer must be text")
    cursor = 0
    grounded: list[dict[str, Any]] = []
    for position, name in enumerate(names, start=1):
        if not isinstance(name, str) or not name:
            raise MentionExtractionError("grounded names must be non-empty strings")
        start = raw_answer.find(name, cursor)
        if start < 0:
            raise MentionExtractionError(
                f"name at position {position} cannot be matched exactly at or after "
                "the preceding occurrence"
            )
        end = start + len(name)
        grounded.append(
            {
                "position": position,
                "name": name,
                "character_start": start,
                "character_end": end,
                "byte_start": len(raw_answer[:start].encode("utf-8")),
                "byte_end": len(raw_answer[:end].encode("utf-8")),
            }
        )
        cursor = end
    return grounded


def exact_chat_message_budget(
    observations: Sequence[Mapping[str, object]],
    *,
    platform_data_modes: Mapping[str, object] | None = None,
) -> dict[str, int]:
    """Return the exact topology implied by a frozen prompt measurement plan.

    New frozen plans keep platform data modes on the scope manifest rather
    than duplicating them into every hash-bound prompt measurement. Callers planning
    from those artifacts must supply ``scope["platform_data_modes"]`` so
    derived surfaces such as Copilot are correctly budgeted at zero sends.
    """

    conversational_chats = 0
    google_search_submissions = 0
    standalone_extraction_chats = 0
    for observation in observations:
        platform = str(observation.get("platform") or "")
        data_mode = str(
            observation.get("platform_data_mode")
            or observation.get("measurement_mode")
            or (
                platform_data_modes.get(platform)
                if isinstance(platform_data_modes, Mapping)
                else ""
            )
            or ""
        )
        if "derived" in data_mode:
            continue
        if platform in CONVERSATIONAL_PLATFORMS:
            conversational_chats += 1
        elif platform == "google_ai_overview":
            google_search_submissions += 1
            status = str(observation.get("status") or "")
            # Frozen plans have no status and therefore report the maximum
            # approved topology. Collected evidence creates an extractor chat
            # only when an isolated generated overview actually exists.
            capture_version = str(
                observation.get("generated_answer_capture_version") or ""
            )
            if not status or (
                status == "collected"
                and capture_version
                == "google-ai-overview-generated-answer-v1"
            ):
                standalone_extraction_chats += 1
    return {
        "conversational_chats": conversational_chats,
        "turn_1_messages": conversational_chats,
        "turn_2_messages": conversational_chats,
        "conversational_messages": conversational_chats * 2,
        "google_search_submissions": google_search_submissions,
        "standalone_extraction_chats": standalone_extraction_chats,
        "standalone_extraction_messages": standalone_extraction_chats,
        "total_chat_count": conversational_chats + standalone_extraction_chats,
        "total_external_submissions": (
            conversational_chats * 2
            + google_search_submissions
            + standalone_extraction_chats
        ),
    }


def _identity(value: Mapping[str, object], *, require_conversation: bool) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise MentionExtractionError("chat identity must be a mapping")
    required = (
        "profile_id",
        "chrome_directory",
        "browser_context_reference",
        "tab_reference",
    )
    result: dict[str, str] = {}
    for key in required:
        item = str(value.get(key) or "").strip()
        if not item:
            raise MentionExtractionError(f"chat identity requires {key}")
        result[key] = item
    conversation = str(value.get("conversation_reference") or "").strip()
    if require_conversation and not conversation:
        raise MentionExtractionError("chat identity requires conversation_reference")
    result["conversation_reference"] = conversation
    return result


def _same_tab(expected: Mapping[str, object], observed: Mapping[str, object]) -> None:
    for key in (
        "profile_id",
        "chrome_directory",
        "browser_context_reference",
        "tab_reference",
    ):
        if str(expected.get(key) or "") != str(observed.get(key) or ""):
            raise MentionExtractionError(f"same-chat identity mismatch for {key}")
    expected_conversation = str(expected.get("conversation_reference") or "")
    observed_conversation = str(observed.get("conversation_reference") or "")
    if expected_conversation and expected_conversation != observed_conversation:
        raise MentionExtractionError(
            "turn 2 must use the exact turn-1 conversation reference"
        )


class MentionExtractionStore:
    """Durable per-prompt measurement turn send attempts and separate response evidence."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def state_path(self, observation_id: str) -> Path:
        return self.root / "states" / f"{_safe_name(observation_id)}.json"

    def response_path(self, observation_id: str, turn: int) -> Path:
        return (
            self.root
            / "responses"
            / _safe_name(observation_id)
            / f"turn-{turn}.txt"
        )

    def load(self, observation_id: str) -> dict[str, Any] | None:
        path = self.state_path(observation_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MentionExtractionError(
                f"invalid durable extraction state for {observation_id}"
            ) from exc
        if not isinstance(value, dict):
            raise MentionExtractionError("durable extraction state must be an object")
        return value

    def initialise(
        self,
        *,
        observation_id: str,
        platform: str,
        original_prompt: str,
        identity: Mapping[str, object],
    ) -> dict[str, Any]:
        if platform not in CONVERSATIONAL_PLATFORMS:
            raise MentionExtractionError(
                f"{platform} does not support same-conversation extraction"
            )
        if not observation_id or not original_prompt:
            raise MentionExtractionError("observation id and original prompt are required")
        observed_identity = _identity(identity, require_conversation=False)
        existing = self.load(observation_id)
        if existing is not None:
            if (
                existing.get("platform") != platform
                or existing.get("turn_1", {}).get("prompt_sha256")
                != _sha256_text(original_prompt)
            ):
                raise MentionExtractionError(
                    "durable extraction state does not match this observation request"
                )
            _same_tab(existing.get("chat_identity") or {}, observed_identity)
            return existing
        state = {
            "schema_version": MENTION_EXTRACTION_SCHEMA_VERSION,
            "prompt_version": MENTION_EXTRACTION_PROMPT_VERSION,
            "observation_id": observation_id,
            "platform": platform,
            "chat_count": 1,
            "message_budget": 2,
            "chat_identity": observed_identity,
            "turn_1": {
                "state": "prepared",
                "prompt_sha256": _sha256_text(original_prompt),
                "send_started_at": None,
                "send_identity": None,
                "response_captured_at": None,
                "acknowledged_at": None,
                "ack_identity": None,
                "response_reference": None,
                "response_sha256": None,
            },
            "turn_2": {
                "state": "prepared",
                "prompt_version": MENTION_EXTRACTION_PROMPT_VERSION,
                "prompt_sha256": _sha256_text(MENTION_EXTRACTION_PROMPT),
                "send_started_at": None,
                "send_identity": None,
                "acknowledged_at": None,
                "ack_identity": None,
                "response_reference": None,
                "response_sha256": None,
                "validation_status": None,
                "validation_error": None,
            },
            "grounded_occurrences": None,
        }
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def mark_non_conversational(
        self,
        *,
        observation_id: str,
        platform: str,
        raw_answer: str,
        profile_id: str,
        chrome_directory: str,
        browser_context_reference: str,
        surface_reference: str,
        capture_contract_version: str | None = None,
        citations: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, Any]:
        if platform not in NON_CONVERSATIONAL_PLATFORMS:
            raise MentionExtractionError(f"{platform} is not a non-conversational surface")
        existing = self.load(observation_id)
        if existing is not None:
            if str(existing.get("platform") or "") != platform:
                raise MentionExtractionError(
                    "durable non-conversational source platform changed"
                )
            if self.read_response(observation_id, turn=1) != raw_answer:
                raise MentionExtractionError(
                    "durable non-conversational source answer is immutable"
                )
            stored_capture_version = str(
                (existing.get("turn_1") or {}).get(
                    "capture_contract_version"
                )
                or ""
            )
            requested_capture_version = str(
                capture_contract_version or ""
            )
            if (
                requested_capture_version
                and stored_capture_version != requested_capture_version
            ):
                raise MentionExtractionError(
                    "durable non-conversational capture contract changed"
                )
            return existing
        response_path = self.response_path(observation_id, 1)
        _atomic_text(response_path, raw_answer)
        state = {
            "schema_version": MENTION_EXTRACTION_SCHEMA_VERSION,
            "prompt_version": MENTION_EXTRACTION_PROMPT_VERSION,
            "observation_id": observation_id,
            "platform": platform,
            "chat_count": 0,
            "message_budget": 1,
            "surface_identity": {
                "profile_id": profile_id,
                "chrome_directory": chrome_directory,
                "browser_context_reference": browser_context_reference,
                "surface_reference": surface_reference,
            },
            "turn_1": {
                "state": "acknowledged",
                "response_reference": str(response_path.relative_to(self.root)),
                "response_sha256": _sha256_text(raw_answer),
                "capture_contract_version": (
                    str(capture_contract_version or "") or None
                ),
                "citations": [
                    dict(row)
                    for row in citations
                    if isinstance(row, Mapping)
                ],
            },
            "turn_2": {
                "state": "unsupported",
                "reason": (
                    "Google AI Overview has no same-chat second turn. Its approved "
                    "source-bound Gemini extraction is stored in a separate "
                    "cross-surface evidence contract."
                ),
            },
            "grounded_occurrences": None,
        }
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def begin_turn_1(self, observation_id: str) -> dict[str, Any]:
        state = self._required(observation_id)
        current = str(state.get("turn_1", {}).get("state") or "")
        if current == "sending":
            raise AmbiguousTurnError("turn 1 has an ambiguous durable send fence")
        if current != "prepared":
            raise MentionExtractionError(f"turn 1 cannot begin from state {current!r}")
        state["turn_1"]["state"] = "sending"
        state["turn_1"]["send_started_at"] = _utc_now()
        state["turn_1"]["send_identity"] = deepcopy(
            state.get("chat_identity") or {}
        )
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def acknowledge_turn_1(
        self,
        observation_id: str,
        raw_answer: str,
        *,
        identity: Mapping[str, object],
    ) -> dict[str, Any]:
        state = self._required(observation_id)
        current = str(state.get("turn_1", {}).get("state") or "")
        if current == "sending":
            self.capture_turn_1_response(observation_id, raw_answer)
            state = self._required(observation_id)
            current = str(state.get("turn_1", {}).get("state") or "")
        if current != "response_captured":
            raise MentionExtractionError("turn 1 acknowledgement requires a send fence")
        if self.read_response(observation_id, turn=1) != raw_answer:
            raise MentionExtractionError(
                "turn 1 acknowledgement differs from captured response evidence"
            )
        observed_identity = _identity(identity, require_conversation=True)
        _same_tab(state.get("chat_identity") or {}, observed_identity)
        state["chat_identity"] = observed_identity
        state["turn_1"].update(
            {
                "state": "acknowledged",
                "acknowledged_at": _utc_now(),
                "ack_identity": observed_identity,
            }
        )
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def capture_turn_1_response(
        self,
        observation_id: str,
        raw_answer: str,
    ) -> dict[str, Any]:
        """Make the exact answer durable before any other browser interaction."""

        state = self._required(observation_id)
        if state.get("turn_1", {}).get("state") != "sending":
            raise MentionExtractionError(
                "turn 1 response capture requires a send fence"
            )
        response_path = self.response_path(observation_id, 1)
        _atomic_text(response_path, raw_answer)
        state["turn_1"].update(
            {
                "state": "response_captured",
                "response_captured_at": _utc_now(),
                "response_reference": str(response_path.relative_to(self.root)),
                "response_sha256": _sha256_text(raw_answer),
            }
        )
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def begin_turn_2(
        self,
        observation_id: str,
        *,
        identity: Mapping[str, object],
    ) -> dict[str, Any]:
        state = self._required(observation_id)
        if state.get("turn_1", {}).get("state") != "acknowledged":
            raise MentionExtractionError(
                "turn 2 requires a durably acknowledged turn 1"
            )
        self.read_response(observation_id, turn=1)
        observed_identity = _identity(identity, require_conversation=True)
        _same_tab(state.get("chat_identity") or {}, observed_identity)
        current = str(state.get("turn_2", {}).get("state") or "")
        if current == "sending":
            raise AmbiguousTurnError("turn 2 has an ambiguous durable send fence")
        if current != "prepared":
            raise MentionExtractionError(f"turn 2 cannot begin from state {current!r}")
        state["turn_2"]["state"] = "sending"
        state["turn_2"]["send_started_at"] = _utc_now()
        state["turn_2"]["send_identity"] = observed_identity
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def acknowledge_turn_2(
        self,
        observation_id: str,
        response: str,
        *,
        identity: Mapping[str, object],
    ) -> dict[str, Any]:
        state = self._required(observation_id)
        if state.get("turn_2", {}).get("state") != "sending":
            raise MentionExtractionError("turn 2 acknowledgement requires a send fence")
        observed_identity = _identity(identity, require_conversation=True)
        _same_tab(state.get("chat_identity") or {}, observed_identity)
        response_path = self.response_path(observation_id, 2)
        _atomic_text(response_path, response)
        state["turn_2"].update(
            {
                "state": "acknowledged",
                "acknowledged_at": _utc_now(),
                "ack_identity": observed_identity,
                "response_reference": str(response_path.relative_to(self.root)),
                "response_sha256": _sha256_text(response),
            }
        )
        try:
            names = parse_closed_name_array(response)
            raw_answer = self.read_response(observation_id, turn=1)
            occurrences = find_name_positions(raw_answer, names)
        except MentionExtractionError as exc:
            state["turn_2"]["validation_status"] = "rejected"
            state["turn_2"]["validation_error"] = str(exc)
            state["grounded_occurrences"] = None
            _atomic_json(self.state_path(observation_id), state)
            raise
        state["turn_2"]["validation_status"] = "complete"
        state["turn_2"]["validation_error"] = None
        state["grounded_occurrences"] = occurrences
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def record_error(self, observation_id: str, detail: object) -> dict[str, Any]:
        state = self._required(observation_id)
        state["last_error"] = str(detail)
        state["last_error_at"] = _utc_now()
        _atomic_json(self.state_path(observation_id), state)
        return deepcopy(state)

    def resume_action(self, observation_id: str) -> str:
        state = self._required(observation_id)
        turn_1 = str(state.get("turn_1", {}).get("state") or "")
        turn_2 = str(state.get("turn_2", {}).get("state") or "")
        if turn_1 == "prepared":
            return "send_turn_1"
        if turn_1 == "sending":
            return "refuse_ambiguous_turn_1"
        if turn_1 == "response_captured":
            return "refuse_ambiguous_turn_1"
        if turn_1 != "acknowledged":
            return "blocked"
        self.read_response(observation_id, turn=1)
        if turn_2 == "prepared":
            return "send_turn_2"
        if turn_2 == "sending":
            return "refuse_ambiguous_turn_2"
        if turn_2 == "acknowledged":
            return (
                "complete"
                if state.get("turn_2", {}).get("validation_status") == "complete"
                else "rejected"
            )
        if turn_2 == "unsupported":
            return "unsupported"
        return "blocked"

    def read_response(self, observation_id: str, *, turn: int) -> str:
        if turn not in (1, 2):
            raise MentionExtractionError("turn must be 1 or 2")
        state = self._required(observation_id)
        row = state.get(f"turn_{turn}") or {}
        reference = str(row.get("response_reference") or "")
        expected = str(row.get("response_sha256") or "")
        if not reference or not expected:
            raise MentionExtractionError(f"turn {turn} response evidence is incomplete")
        path = self.root / reference
        if not path.is_file():
            raise MentionExtractionError(f"turn {turn} response evidence is missing")
        value = _read_text_losslessly(path)
        if _sha256_text(value) != expected:
            raise MentionExtractionError(f"turn {turn} response hash does not match")
        return value

    def iter_states(self) -> list[dict[str, Any]]:
        states_dir = self.root / "states"
        if not states_dir.is_dir():
            return []
        result = []
        for path in sorted(states_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MentionExtractionError(
                    f"invalid durable extraction state: {path}"
                ) from exc
            if not isinstance(value, dict):
                raise MentionExtractionError(f"extraction state is not an object: {path}")
            result.append(value)
        return result

    def _required(self, observation_id: str) -> dict[str, Any]:
        state = self.load(observation_id)
        if state is None:
            raise MentionExtractionError(
                f"no durable extraction state exists for {observation_id}"
            )
        return state


__all__ = [
    "AmbiguousTurnError",
    "CONVERSATIONAL_PLATFORMS",
    "MENTION_EXTRACTION_PROMPT",
    "MENTION_EXTRACTION_PROMPT_VERSION",
    "MENTION_EXTRACTION_SCHEMA_VERSION",
    "MentionExtractionError",
    "MentionExtractionStore",
    "NON_CONVERSATIONAL_PLATFORMS",
    "find_name_positions",
    "exact_chat_message_budget",
    "parse_closed_name_array",
]
