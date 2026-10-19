"""Filename -> Kitsu entity matching.

Pure functions only (no Qt, no network) so this can be unit-tested.

Strategy
--------
Filenames are split into tokens on separators and on letter/digit
boundaries, and numeric parts lose their zero padding, so that
``EP01_SQ010-SH0020_anim_v003.mov`` yields ``ep1 sq10 sh20 anim v3 mov``.

* Shots match on their shot token; a matching sequence token raises the
  score, and is required to disambiguate when the same shot name exists in
  several sequences.
* Assets match when their name's tokens appear consecutively in the
  filename; a weaker "squashed substring" match catches ``HeroBob`` vs
  ``hero_bob``. The longest matching asset name wins (``TreeA`` over
  ``Tree``).

A match below ``AUTO_THRESHOLD`` or tied with another candidate is not
auto-assigned; it goes to the unassigned pool with its candidates listed
as suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

AUTO_THRESHOLD = 0.75

_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_PART_RE = re.compile(r"[a-z]+|\d+")
_VERSION_RE = re.compile(r"(?:^|[^a-z])v(\d{1,4})(?![a-z])", re.IGNORECASE)


@dataclass
class Candidate:
    entity_id: str
    label: str
    score: float


@dataclass
class MatchResult:
    entity_id: str | None
    score: float
    reason: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def is_auto(self) -> bool:
        return self.entity_id is not None


def tokenize(text: str) -> list[str]:
    """Lowercase, split on separators and letter/digit boundaries, strip
    zero padding: ``"SQ010_sh0020"`` -> ``["sq10", "sh20"]`` after pairing.

    Letter runs directly followed by a digit run are kept together as one
    token (``sh20``) because that is how production names are built.
    """
    tokens: list[str] = []
    for chunk in _SPLIT_RE.split(text.lower()):
        if not chunk:
            continue
        parts = _PART_RE.findall(chunk)
        i = 0
        while i < len(parts):
            part = parts[i]
            if part.isdigit():
                tokens.append(str(int(part)))
                i += 1
            elif i + 1 < len(parts) and parts[i + 1].isdigit():
                tokens.append(part + str(int(parts[i + 1])))
                i += 2
            else:
                tokens.append(part)
                i += 1
    return tokens


def squash(text: str) -> str:
    return "".join(tokenize(text))


def strip_ext(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def parse_version(filename: str) -> int | None:
    m = None
    for m in _VERSION_RE.finditer(strip_ext(filename)):
        pass
    return int(m.group(1)) if m else None


def _numeric_part(token: str | None) -> str | None:
    if token is None:
        return None
    m = re.fullmatch(r"[a-z]*(\d+)", token)
    return m.group(1) if m else None


def _find(name_tokens: list[str], file_tokens: list) -> int:
    n = len(name_tokens)
    for i in range(len(file_tokens) - n + 1):
        if file_tokens[i : i + n] == name_tokens:
            return i
    return -1


def _token_hit(name_tokens: list[str], file_tokens: list) -> float:
    """1.0 if the name's tokens appear consecutively in the file tokens.
    0.85 if they only match by number (``0020`` vs ``sh20``).
    ``file_tokens`` may contain ``None`` for masked-out positions."""
    if not name_tokens:
        return 0.0
    if _find(name_tokens, file_tokens) >= 0:
        return 1.0
    if len(name_tokens) == 1:
        num = _numeric_part(name_tokens[0])
        bare_name = name_tokens[0].isdigit()
        if num is not None:
            for tok in file_tokens:
                if tok is not None and _numeric_part(tok) == num and (
                    bare_name or tok.isdigit()
                ):
                    return 0.85
    return 0.0


def match_shot(filename: str, shots: list[dict]) -> MatchResult:
    """``shots`` items need ``id``, ``name``, ``sequence_name`` and
    optionally ``episode_name`` (used to separate identical SQ/SH names
    across episodes)."""
    file_tokens = tokenize(strip_ext(filename))
    scored: list[Candidate] = []
    for shot in shots:
        shot_tokens = tokenize(shot["name"])
        seq = shot.get("sequence_name") or ""
        seq_tokens = tokenize(seq)
        # Shot names that already embed the sequence (SQ010_SH020) get the
        # sequence credit from the shot hit itself.
        embedded = (
            0 < len(seq_tokens) < len(shot_tokens)
            and _find(seq_tokens, shot_tokens) >= 0
        )
        seq_pos = _find(seq_tokens, file_tokens) if seq_tokens else -1
        ep_tokens = tokenize(shot.get("episode_name") or "")
        ep_pos = _find(ep_tokens, file_tokens) if ep_tokens else -1
        available: list = list(file_tokens)
        if seq_pos >= 0 and not embedded:
            # The sequence token can't also be the shot token ("010_0020").
            for i in range(seq_pos, seq_pos + len(seq_tokens)):
                available[i] = None
        if ep_pos >= 0:
            for i in range(ep_pos, ep_pos + len(ep_tokens)):
                available[i] = None
        shot_score = _token_hit(shot_tokens, available)
        if shot_score == 0:
            continue
        if embedded:
            seq_score = shot_score
        else:
            seq_score = 1.0 if seq_pos >= 0 else _token_hit(seq_tokens, file_tokens)
        score = shot_score * (0.8 + 0.2 * seq_score) + (0.05 if ep_pos >= 0 else 0.0)
        ep = shot.get("episode_name") or ""
        label = " / ".join(x for x in (ep, seq, shot["name"]) if x)
        scored.append(Candidate(shot["id"], label, round(score, 3)))
    return _decide(scored)


def match_asset(filename: str, assets: list[dict]) -> MatchResult:
    """``assets`` items need ``id``, ``name`` and optionally ``asset_type_name``."""
    stem = strip_ext(filename)
    file_tokens = tokenize(stem)
    file_squashed = "".join(file_tokens)
    scored: list[Candidate] = []
    for asset in assets:
        name_tokens = tokenize(asset["name"])
        if not name_tokens:
            continue
        score = _token_hit(name_tokens, file_tokens)
        if score == 0:
            name_squashed = "".join(name_tokens)
            if _joins_tokens(name_squashed, file_tokens):
                score = 0.9  # "HeroBob" vs "hero_bob": same name, other separators
            elif len(name_squashed) >= 4 and name_squashed in file_squashed:
                score = 0.6  # buried inside a longer word: suggest only
        if score == 0:
            continue
        # Prefer longer, more specific names: "TreeA" over "Tree".
        specificity = min(len("".join(name_tokens)) / 40.0, 0.05)
        atype = asset.get("asset_type_name") or ""
        label = f"{atype} / {asset['name']}" if atype else asset["name"]
        scored.append(Candidate(asset["id"], label, round(score + specificity, 3)))
    return _decide(scored)


def _joins_tokens(squashed: str, file_tokens: list[str]) -> bool:
    """True if some run of consecutive file tokens concatenates to ``squashed``."""
    for i in range(len(file_tokens)):
        acc = ""
        for tok in file_tokens[i:]:
            acc += tok
            if acc == squashed:
                return True
            if len(acc) >= len(squashed):
                break
    return False


def _decide(scored: list[Candidate]) -> MatchResult:
    scored.sort(key=lambda c: c.score, reverse=True)
    if not scored:
        return MatchResult(None, 0.0, "No name match", [])
    best = scored[0]
    if len(scored) > 1 and abs(scored[1].score - best.score) < 0.01:
        names = ", ".join(c.label for c in scored[:3])
        return MatchResult(None, best.score, f"Ambiguous: {names}", scored[:8])
    if best.score < AUTO_THRESHOLD:
        return MatchResult(None, best.score, f"Weak match: {best.label}", scored[:8])
    return MatchResult(best.entity_id, best.score, f"Matched {best.label}", scored[:8])


def match(filename: str, entities: list[dict], entity_kind: str) -> MatchResult:
    if entity_kind == "shot":
        return match_shot(filename, entities)
    return match_asset(filename, entities)
