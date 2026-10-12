"""Read-only adapters for archived Snapshot evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..errors import IntegrityError
from ..planning.hashing import bytes_sha256, text_sha256
from .filesystem import read_text_losslessly


class LegacyEvidenceReader:
    """Inspect known v2/v3/v4 and detached name listing artifacts without writes."""

    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)

    def scope(self) -> Optional[dict[str, Any]]:
        return self._object("scope_manifest.json")

    def prompts(self) -> Optional[dict[str, Any]]:
        return self._object("snapshot_prompts.json")

    def observation_plan(self) -> Optional[dict[str, Any]]:
        return self._object("observation_plan.json")

    def runner_state(self) -> Optional[dict[str, Any]]:
        # Package-owned runs use runner_state.json.  Archived application runs
        # used snapshot_state.json; returning it as a fallback keeps callers
        # read-only while avoiding version-specific branching.
        return self._first_object("runner_state.json", "snapshot_state.json")

    def snapshot_state(self) -> Optional[dict[str, Any]]:
        """Return the exact archived application state, when present."""

        return self._object("snapshot_state.json")

    def same_chat_state(self, platform: str, observation_id: str) -> Optional[dict[str, Any]]:
        root = (
            self.run_dir / "platforms" / platform / "mention_extraction"
        )
        return self._state(root, observation_id)

    def gaio_extractor_state(
        self,
        observation_id: str,
    ) -> Optional[dict[str, Any]]:
        """Read a source-bound archived GAIO Gemini extractor state.

        The first path is the frozen application layout.  The shorter paths
        cover early v4 candidates that were archived before the redundant
        surface directory was standardised.
        """

        roots = (
            self.run_dir
            / "platforms"
            / "google_ai_overview"
            / "google_ai_overview"
            / "gemini_name_extraction",
            self.run_dir
            / "platforms"
            / "google_ai_overview"
            / "gemini_name_extraction",
            self.run_dir
            / "platforms"
            / "google_ai_overview"
            / "gaio_gemini_extraction",
        )
        found: list[dict[str, Any]] = []
        for root in roots:
            value = self._state(root, observation_id)
            if value is not None:
                found.append(value)
        if len(found) > 1:
            raise IntegrityError(
                "archived GAIO extractor state is present in more than one "
                f"known layout: {observation_id}"
            )
        return found[0] if found else None

    # Compatibility spellings used by application-side migration adapters.
    gaio_gemini_state = gaio_extractor_state
    google_ai_extractor_state = gaio_extractor_state

    def observation(
        self,
        platform: str,
        observation_id: str,
    ) -> Optional[dict[str, Any]]:
        """Read one archived prompt measurement from JSON, JSONL, or directory layouts."""

        platform_root = self.run_dir / "platforms" / platform
        candidates = (
            platform_root / "observations" / f"{observation_id}.json",
            platform_root / "observations.json",
            platform_root / "observations.jsonl",
        )
        found: list[dict[str, Any]] = []
        for path in candidates:
            if not path.is_file():
                continue
            if path.suffix == ".jsonl":
                rows = self._read_json_lines(path)
            else:
                value = self._read_json(path)
                if isinstance(value, list):
                    rows = value
                elif isinstance(value, Mapping):
                    nested = value.get("observations")
                    rows = nested if isinstance(nested, list) else [value]
                else:
                    raise IntegrityError(
                        f"archived observations must contain objects: {path}"
                    )
            for row in rows:
                if not isinstance(row, Mapping):
                    raise IntegrityError(
                        f"archived observation must be an object: {path}"
                    )
                if str(row.get("observation_id") or "") == observation_id:
                    found.append(dict(row))
        if len(found) > 1:
            raise IntegrityError(
                f"duplicate archived observation {observation_id!r} for {platform}"
            )
        if not found:
            return None
        value = found[0]
        self._verify_observation_hashes(value)
        return value

    def gaio_observation(
        self,
        observation_id: str,
    ) -> Optional[dict[str, Any]]:
        return self.observation("google_ai_overview", observation_id)

    def detached_batch_state(self, platform: str) -> Optional[dict[str, Any]]:
        for relative in (
            Path("platforms") / platform / "name_reconciliation" / "state.json",
            Path("platforms") / platform / "name_reconciliation" / "result.json",
            Path("platforms")
            / platform
            / "name_reconciliation"
            / "transport_result.json",
            Path("platforms") / platform / "extraction" / "state.json",
            Path("platforms") / platform / "extraction" / "result.json",
        ):
            path = self.run_dir / relative
            if path.is_file():
                value = self._read_object(path)
                value = dict(value)
                value["legacy_read_only"] = True
                value["eligible_for_new_run_fallback"] = False
                return value
        return None

    def inspect(self) -> dict[str, Any]:
        return {
            "scope_manifest": self.scope(),
            "snapshot_prompts": self.prompts(),
            "observation_plan": self.observation_plan(),
            "runner_state": self.runner_state(),
            "snapshot_state": self.snapshot_state(),
            "read_only": True,
        }

    def _object(self, relative: str) -> Optional[dict[str, Any]]:
        path = self.run_dir / relative
        if not path.is_file():
            return None
        return self._read_object(path)

    def _first_object(self, *relatives: str) -> Optional[dict[str, Any]]:
        for relative in relatives:
            value = self._object(relative)
            if value is not None:
                return value
        return None

    def _state(
        self,
        root: Path,
        observation_id: str,
    ) -> Optional[dict[str, Any]]:
        found: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted((root / "states").glob("*.json")):
            value = self._read_object(path)
            if str(value.get("observation_id") or "") == observation_id:
                found.append((path, value))
        if len(found) > 1:
            raise IntegrityError(
                f"duplicate archived state for observation {observation_id!r}: {root}"
            )
        if not found:
            return None
        path, value = found[0]
        self._verify_response_hashes(root, value)
        return value

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(read_text_losslessly(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"invalid archived evidence {path}") from exc

    @staticmethod
    def _read_object(path: Path) -> dict[str, Any]:
        value = LegacyEvidenceReader._read_json(path)
        if not isinstance(value, dict):
            raise IntegrityError(f"archived evidence must contain an object: {path}")
        return value

    @staticmethod
    def _read_json_lines(path: Path) -> list[Any]:
        try:
            lines = read_text_losslessly(path).splitlines()
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"invalid archived evidence {path}") from exc

    def _verify_observation_hashes(self, observation: Mapping[str, Any]) -> None:
        reference = str(observation.get("raw_answer_reference") or "")
        text_hash = str(observation.get("raw_answer_sha256") or "")
        file_hash = str(observation.get("raw_answer_file_sha256") or "")
        if not reference or not (text_hash or file_hash):
            return
        path = self._evidence_path(self.run_dir, reference)
        try:
            payload = path.read_bytes()
            value = read_text_losslessly(path)
        except OSError as exc:
            raise IntegrityError(f"missing archived response {path}") from exc
        if text_hash and text_sha256(value) != text_hash:
            raise IntegrityError(f"archived response text hash mismatch: {path}")
        if file_hash and bytes_sha256(payload) != file_hash:
            raise IntegrityError(f"archived response file hash mismatch: {path}")

    @staticmethod
    def _verify_response_hashes(root: Path, state: Mapping[str, Any]) -> None:
        for turn_key in ("turn_1", "turn_2", "source", "extractor"):
            turn = state.get(turn_key)
            if not isinstance(turn, Mapping):
                continue
            reference = str(turn.get("response_reference") or "")
            expected = str(turn.get("response_sha256") or "")
            if not reference or not expected:
                continue
            path = LegacyEvidenceReader._evidence_path(root, reference)
            try:
                value = read_text_losslessly(path)
            except OSError as exc:
                raise IntegrityError(f"missing archived response {path}") from exc
            if text_sha256(value) != expected:
                raise IntegrityError(f"archived response hash mismatch: {path}")

    @staticmethod
    def _evidence_path(root: Path, reference: str) -> Path:
        resolved_root = root.resolve()
        path = (resolved_root / reference).resolve()
        if not path.is_relative_to(resolved_root):
            raise IntegrityError(
                f"archived evidence reference escapes its root: {reference}"
            )
        return path


__all__ = ["LegacyEvidenceReader"]
