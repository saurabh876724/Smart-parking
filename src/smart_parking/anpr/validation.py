"""Plate text normalisation and validation (Phase 5).

OCR hands back strings, not plates. This layer decides whether a string *is* a
plausible Indian registration number, and repairs OCR homoglyphs without ever
changing the length of what it was given - the two rules that keep a garbled
read from becoming a confident wrong answer.

Rejected text is returned as `plate=None` with a reason, so the caller can
count it as a vote for "nothing" instead of silently dropping evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations

from smart_parking.core.config import AnprValidationConfig
from smart_parking.core.logging import get_logger

log = get_logger("anpr.validation")

# State / union-territory codes that may open a registration number. Used only
# when `validation.require_region_code` is on, because the list can lag behind
# new series and rejecting a real plate is worse than accepting an odd one.
REGION_CODES: frozenset[str] = frozenset(
    {
        "AH", "AN", "AP", "AR", "AS", "BH", "BR", "CC", "CG", "CH", "DL", "GA",
        "GJ", "HP", "HR", "IN", "JH", "JK", "KA", "KL", "LA", "MH", "ML", "MN",
        "MP", "MZ", "NL", "OR", "PB", "PY", "RJ", "SK", "TN", "TR", "TS", "UP",
        "UT", "WB",
    }
)

# Shapes of a valid plate. Kept as data so the module can say *why* it accepted
# something and a deployment can add a series without a code change.
PATTERNS: tuple[tuple[str, str], ...] = (
    # Current format: region + district, optional series letters, 4 random
    # digits. Covers MH20AB1234, TS09D1234, DL12ABCD1234, AB12BH3456 and the
    # trailing international-mark variants.
    ("bharat_series", r"[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}(?:I|IND)?"),
    # Pre-1987 central-government style, e.g. 23AH1234.
    ("central_old", r"[0-9]{2}[A-Z]{1,2}[0-9]{4}"),
)

_SEPARATORS = re.compile(r"[^A-Z0-9]+")

# Devanagari and fullwidth digits appear on regional plates and in some OCR
# output; they normalise to ASCII exactly, which is a translation not a guess.
_DIGIT_ALIASES = {
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
}

# Characters OCR reads for a *digit*. Applied only where a pattern demands a
# digit, so 'S' in a series letter is never touched.
DIGIT_HOMOGLYPHS: dict[str, str] = {
    "O": "0",
    "Q": "0",
    "D": "0",
    "I": "1",
    "L": "1",
    "Z": "2",
    "S": "5",
    "B": "8",
    "G": "6",
    "T": "7",
}

# The mirror case: a digit read where the plate has a letter. Far more likely to
# corrupt a valid plate than to rescue one, so it is off by default.
LETTER_HOMOGLYPHS: dict[str, str] = {
    "0": "O",
    "1": "I",
    "2": "Z",
    "5": "S",
    "6": "G",
    "7": "T",
    "8": "B",
}


@dataclass(frozen=True)
class ValidationOutcome:
    raw: str
    normalised: str
    plate: str | None
    pattern: str | None
    edits: int = 0

    @property
    def accepted(self) -> bool:
        return self.plate is not None

    @property
    def reason(self) -> str:
        if self.accepted:
            return "ok"
        if not self.normalised:
            return "empty_after_normalisation"
        if len(self.normalised) < 6:
            return "too_short"
        return "no_pattern_match"


class PlateValidator:
    def __init__(self, cfg: AnprValidationConfig) -> None:
        self.cfg = cfg
        self._patterns = list(PATTERNS) + [
            ("custom", p) for p in cfg.extra_patterns
        ]
        # Anchored, full-match: a plate that merely *contains* a valid shape is
        # not a valid plate.
        self._compiled = [(name, re.compile(f"^(?:{p})$")) for name, p in self._patterns]

    def normalise(self, text: str) -> str:
        """Uppercase, translate regional digit forms, drop separators.

        No character is ever substituted or added here: digits are only
        re-translated from an exact alias table, so the length of the plate is
        preserved unless the text carries printed country words.
        """
        if not text:
            return ""
        out = []
        for ch in text.upper():
            out.append(_DIGIT_ALIASES.get(ch, ch))
        return self._strip_words(_SEPARATORS.sub("", "".join(out)))

    def _strip_words(self, normalised: str) -> str:
        """Remove printed country markers at the edges, never inside the text."""
        changed = True
        while changed:
            changed = False
            for word in self.cfg.strip_words:
                token = word.upper()
                if normalised.startswith(token):
                    normalised = normalised[len(token):]
                    changed = True
                elif normalised.endswith(token):
                    normalised = normalised[: -len(token)]
                    changed = True
        return normalised

    def validate(self, text: str) -> ValidationOutcome:
        normalised = self.normalise(text)
        if not self._sized(normalised):
            return ValidationOutcome(raw=text, normalised=normalised, plate=None, pattern=None)

        match = self._match(normalised)
        if match is not None:
            return ValidationOutcome(raw=text, normalised=normalised, plate=normalised, pattern=match)

        if self.cfg.repair_confusables and self.cfg.max_repair_edits > 0:
            repaired = self._repair(normalised)
            if repaired is not None:
                plate, pattern, edits = repaired
                return ValidationOutcome(
                    raw=text, normalised=plate, plate=plate, pattern=pattern, edits=edits
                )

        return ValidationOutcome(raw=text, normalised=normalised, plate=None, pattern=None)

    def is_valid(self, text: str) -> bool:
        return self.validate(text).accepted

    def _sized(self, normalised: str) -> bool:
        return self.cfg.min_length <= len(normalised) <= self.cfg.max_length

    def _match(self, normalised: str) -> str | None:
        for name, pattern in self._compiled:
            if pattern.match(normalised):
                if self.cfg.require_region_code and not self._has_region_code(normalised):
                    continue
                return name
        return None

    def _has_region_code(self, normalised: str) -> bool:
        # Old central-government plates start with digits, so they have no
        # region code to check and are matched by their own pattern.
        if normalised[:1].isdigit():
            return True
        return normalised[:2] in REGION_CODES

    def _repair(self, normalised: str) -> tuple[str, str, int] | None:
        """Try homoglyph substitutions, accept only a single unambiguous result.

        Two guards keep this from inventing a plate: the reading must already be
        a full-length one (a short string means a character was dropped, and
        re-reading a letter as a digit would silently "fix" the length by
        corrupting the number), and exactly one distinct result may match. Every
        candidate keeps the original length, so nothing is added, dropped or
        reordered.
        """
        if len(normalised) not in self.cfg.repair_lengths:
            return None

        swap = dict(DIGIT_HOMOGLYPHS)
        if self.cfg.repair_letter_positions:
            swap.update(LETTER_HOMOGLYPHS)

        positions = [i for i, ch in enumerate(normalised) if ch in swap]
        if not positions:
            return None

        found: list[tuple[str, str, int]] = []
        for edits in range(1, min(self.cfg.max_repair_edits, len(positions)) + 1):
            for subset in combinations(positions, edits):
                chars = list(normalised)
                for i in subset:
                    chars[i] = swap[chars[i]]
                candidate = "".join(chars)
                if candidate == normalised:
                    continue
                pattern = self._match(candidate)
                if pattern is not None:
                    found.append((candidate, pattern, edits))
            # Stop at the smallest edit count that resolves the text: a 1-edit
            # read is more believable than a 2-edit one.
            if found:
                break

        distinct = {plate for plate, _, _ in found}
        if len(distinct) != 1:
            if len(distinct) > 1:
                log.debug("%s is ambiguous after repair: %s", normalised, sorted(distinct))
            return None
        return found[0][0], found[0][1], found[0][2]
