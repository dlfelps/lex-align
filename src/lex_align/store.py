from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Optional

import frontmatter

from .models import Decision, Provenance, Scope, Status

_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for", "of", "and",
    "or", "but", "not", "we", "use", "used", "by", "with", "this", "was",
    "has", "have", "had", "be", "been", "as", "are", "were", "that", "from",
    "its", "our", "their", "there", "these", "those", "which", "when", "where",
    "how", "all", "would", "could", "should", "may", "might", "can", "will",
    "do", "did", "does", "if", "so", "no", "any", "up", "out", "about", "into",
    "than", "more", "also", "each", "both", "new", "per", "via", "add",
    "get", "set", "run", "make", "take", "give",
})

STOP_WORDS = _STOP_WORDS


def tokenize(text: str) -> set[str]:
    """Split text into lowercase tokens, filtering single-character tokens."""
    return {w.lower() for w in re.split(r"[^a-zA-Z0-9]+", text) if len(w) > 1}


def _stem(word: str) -> str:
    for suffix in ("ings", "tions", "tion", "ations", "ation", "ness", "ied",
                   "ies", "ing", "ed", "ly", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) > 2:
            return word[: -len(suffix)]
    return word


class DecisionStore:
    def __init__(self, decisions_dir: Path):
        self.decisions_dir = decisions_dir

    @property
    def _index_path(self) -> Path:
        return self.decisions_dir.parent / "index.json"

    def load_all(self) -> list[Decision]:
        if not self.decisions_dir.exists():
            return []
        decisions = []
        for path in sorted(self.decisions_dir.glob("ADR-*.md")):
            try:
                decisions.append(self._read(path))
            except Exception:
                pass
        return decisions

    def get(self, adr_id: str) -> Optional[Decision]:
        adr_id = adr_id.upper()
        if not adr_id.startswith("ADR-"):
            adr_id = f"ADR-{adr_id}"
        for path in self.decisions_dir.glob(f"{adr_id}-*.md"):
            return self._read(path)
        return None

    def save(self, decision: Decision) -> Path:
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        path = self.decisions_dir / decision.filename
        body = _build_body(decision)
        post = frontmatter.Post(body, **decision.to_frontmatter())
        path.write_text(frontmatter.dumps(post))
        self._update_index(decision)
        return path

    def next_id(self) -> str:
        existing = self.load_all()
        if not existing:
            return "ADR-0001"
        max_num = max(d.num for d in existing)
        return f"ADR-{max_num + 1:04d}"

    def find_covering(self, package_name: str) -> list[Decision]:
        """Find decisions that cover a given package (by title or scope tags)."""
        name_lower = package_name.lower()
        results = []
        for d in self.load_all():
            if d.status in (Status.SUPERSEDED, Status.REJECTED):
                continue
            if name_lower in d.title.lower():
                results.append(d)
                continue
            if name_lower in [t.lower() for t in d.scope.tags]:
                results.append(d)
        return results

    def history(self, path_or_tag: str) -> list[Decision]:
        """All decisions (including superseded) covering a path or tag."""
        needle = path_or_tag.lower()
        results = []
        for d in self.load_all():
            if needle in [t.lower() for t in d.scope.tags]:
                results.append(d)
                continue
            if any(needle in p.lower() for p in d.scope.paths):
                results.append(d)
        return sorted(results, key=lambda d: d.created)

    def check_constraint(self, tag: str) -> list[tuple[Decision, list]]:
        """Return decisions and alternatives referencing a constraint tag."""
        tag_lower = tag.lower()
        results = []
        for d in self.load_all():
            decision_matches = tag_lower in [c.lower() for c in d.constraints_depended_on]
            alt_matches = [a for a in d.alternatives if a.constraint and tag_lower == a.constraint.lower()]
            if decision_matches or alt_matches:
                results.append((d, alt_matches))
        return results

    # ── Index management ──────────────────────────────────────────────────────

    def _load_index(self) -> dict[str, list[str]]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text())
            except Exception:
                pass
        return {}

    def _save_index(self, index: dict[str, list[str]]) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(index, sort_keys=True))

    def _extract_terms(self, decision: Decision) -> set[str]:
        terms: set[str] = set()
        terms.update(tokenize(decision.title) - _STOP_WORDS)
        terms.update(t.lower() for t in decision.scope.tags)
        terms.update(c.lower() for c in decision.constraints_depended_on)
        for alt in decision.alternatives:
            terms.update(tokenize(alt.name) - _STOP_WORDS)
            if alt.constraint:
                terms.add(alt.constraint.lower())
        if decision.license:
            terms.add(decision.license.lower())
        if decision.provenance is not None:
            terms.add(decision.provenance.value)
        for text in (decision.context_text, decision.decision_text):
            for w in tokenize(text):
                if w not in _STOP_WORDS and len(w) > 2:
                    terms.add(_stem(w))
        return terms

    def _update_index(self, decision: Decision) -> None:
        index = self._load_index()
        for ids in index.values():
            try:
                ids.remove(decision.id)
            except ValueError:
                pass
        index = {k: v for k, v in index.items() if v}
        for term in self._extract_terms(decision):
            if term not in index:
                index[term] = []
            if decision.id not in index[term]:
                index[term].append(decision.id)
        self._save_index(index)

    def rebuild_index(self) -> None:
        """Rebuild the full index from all decision files."""
        index: dict[str, list[str]] = {}
        for decision in self.load_all():
            for term in self._extract_terms(decision):
                if term not in index:
                    index[term] = []
                if decision.id not in index[term]:
                    index[term].append(decision.id)
        self._save_index(index)

    def search_by_terms(self, terms: set[str]) -> list[Decision]:
        """Find decisions matching any of the given terms via the index."""
        index = self._load_index()
        candidate_ids: set[str] = set()
        for term in terms:
            candidate_ids.update(index.get(term, []))
            stemmed = _stem(term)
            if stemmed != term:
                candidate_ids.update(index.get(stemmed, []))
        results = []
        for adr_id in sorted(candidate_ids):
            d = self.get(adr_id)
            if d is not None:
                results.append(d)
        return results

    def _read(self, path: Path) -> Decision:
        post = frontmatter.load(str(path))
        return Decision.from_frontmatter(dict(post.metadata), post.content)


def _build_body(decision: Decision) -> str:
    parts = []
    if decision.context_text:
        parts.append(f"## Context\n{decision.context_text}")
    if decision.decision_text:
        parts.append(f"## Decision\n{decision.decision_text}")
    if decision.consequences_text:
        parts.append(f"## Consequences\n{decision.consequences_text}")
    return "\n\n".join(parts) + "\n" if parts else ""


def create_observed(
    package_name: str,
    store: DecisionStore,
    provenance: Provenance,
    created: Optional[datetime.date] = None,
) -> Decision:
    from .models import Confidence
    decision = Decision(
        id=store.next_id(),
        title=f"Uses {package_name}",
        status=Status.OBSERVED,
        created=created or datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=[package_name]),
        provenance=provenance,
    )
    store.save(decision)
    return decision
