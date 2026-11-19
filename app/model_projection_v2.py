"""Conservative projection of explicitly classified facts for model requests.

This is an allowlist and a contact-data guard, not general anonymization or a
semantic check. Callers must obtain the required authorization before any
provider request; a clean projection is not evidence of consent or safety.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.content_v2 import FactSnapshot

# Producers must classify individual occupational facts with these exact names.
# Unclassified free-form material and unknown types are never sent by default.
MODEL_FACT_TYPES = frozenset({
    "employer", "job_title", "employment_date", "school", "major", "degree",
    "education_date", "skill", "project", "responsibility", "action", "result",
    "qualification", "work_sample", "resource_constraint",
})
_EMAIL = re.compile(r"(?i)[\w.+-]+@[\w.-]+\.[a-z]{2,}")
_URL = re.compile(r"(?i)(?:https?://|www\.)\S+")
# Reject a candidate with a likely telephone number rather than attempting to
# preserve part of the text. This may conservatively reject long numeric dates.
_PHONE = re.compile(r"(?<!\d)[+＋]?\d[\d\s().（）/／\-‐‑‒–—−－]*\d(?!\d)")


def _contains_contact(value: str, private_values: tuple[str, ...]) -> bool:
    folded = value.casefold()
    if any(token.casefold() in folded for token in private_values):
        return True
    if _EMAIL.search(value) or _URL.search(value):
        return True
    for match in _PHONE.finditer(value):
        digits = sum(char.isdecimal() for char in match.group())
        if digits >= 7:
            return True
    return False


_UUID4_HEX = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")


def contains_sensitive_identifier(value: str, private_values: tuple[str, ...]) -> bool:
    """Recognize app UUIDv4 hex IDs without mistaking digit runs for phones.

    Only the canonical lowercase UUID4 hex representation receives this
    exception. A known private value is always rejected, even in a valid ID.
    Arbitrary IDs still receive the complete contact-data check.
    """
    if any(token and token.casefold() in value.casefold() for token in private_values):
        return True
    if _UUID4_HEX.fullmatch(value):
        return False
    return _contains_contact(value, private_values)


@dataclass(frozen=True)
class ProjectionDiagnostics:
    """Local-only exclusions; never include this object in a provider request."""

    excluded: tuple[dict, ...]


@dataclass(frozen=True)
class ProjectionResult:
    model_input: dict
    diagnostics: ProjectionDiagnostics
    # Local-only map, never serialize into a provider request or UI.
    alias_to_claim: Mapping[str, str] | None = None


def project_facts_with_diagnostics(
    snapshot: FactSnapshot,
    *,
    private_header: Mapping[str, str | None],
    additional_private_values: tuple[str, ...] = (),
    alias_identifiers: bool = False,
) -> ProjectionResult:
    """Build a provider payload and separate local-only omission diagnostics.

    Diagnostics contain only claim IDs and category codes, never values. If an
    ID itself looks sensitive, it is suppressed (None) and the ordinal is used
    for local identification. Neither diagnostics nor private inputs belong in
    provider requests or logs. This conservative filter is not anonymization.
    """
    private_values = tuple(
        value.strip() for value in (*private_header.values(), *additional_private_values)
        if isinstance(value, str) and value.strip()
    )
    selected = []
    excluded = []
    aliases: dict[str, str] = {}
    if alias_identifiers:
        # Stable by ordinal and private-value set, independent of random stored
        # UUIDs. Only these aliases ever cross the provider boundary.
        used: set[str] = set()
        prefix = next((char for char in "qabcdefghijklmnopqrstuvwxyz" if not _contains_contact(char, private_values)), None)
        if prefix is None and snapshot.claims:
            raise ValueError("no private-safe model identifier available")
        for claim in snapshot.claims:
            alias = None
            for number in range(20000):
                n = number
                letters = ""
                while True:
                    letters = chr(97 + n % 26) + letters
                    n = n // 26 - 1
                    if n < 0:
                        break
                candidate = prefix + letters
                if candidate not in used and not _contains_contact(candidate, private_values):
                    alias = candidate
                    break
            if alias is None:
                raise ValueError("no private-safe model identifier available")
            aliases[claim.claim_id] = alias
            used.add(alias)
    for ordinal, claim in enumerate(snapshot.claims):
        # Test IDs first so a rejected unsupported type cannot expose a private
        # identifier through the diagnostic path either.
        if not alias_identifiers and contains_sensitive_identifier(claim.claim_id, private_values):
            excluded.append({"claim_id": None, "ordinal": ordinal, "reason": "sensitive_identifier"})
            continue
        diagnostic_id = None if alias_identifiers or contains_sensitive_identifier(claim.claim_id, private_values) else claim.claim_id
        if claim.fact_type not in MODEL_FACT_TYPES:
            excluded.append({"claim_id": diagnostic_id, "ordinal": ordinal, "reason": "unsupported_type"})
            continue
        value = claim.value.strip() if claim.value is not None else ""
        if not value:
            excluded.append({"claim_id": diagnostic_id, "ordinal": ordinal, "reason": "empty_value"})
            continue
        if len(value) > 500:
            excluded.append({"claim_id": diagnostic_id, "ordinal": ordinal, "reason": "value_too_long"})
            continue
        if _contains_contact(value, private_values):
            excluded.append({"claim_id": diagnostic_id, "ordinal": ordinal, "reason": "sensitive_value"})
            continue
        selected.append({
            "claim_id": claim.claim_id,
            "fact_type": claim.fact_type,
            "value": value,
            "confirmation": claim.confirmation,
            "completion": claim.completion,
            "conflict_ids": list(claim.conflict_ids),
        })
    included = {claim["claim_id"] for claim in selected}
    for claim in selected:
        claim["conflict_ids"] = [ref for ref in claim["conflict_ids"] if ref in included]
    if alias_identifiers:
        for claim in selected:
            claim["claim_id"] = aliases[claim["claim_id"]]
            claim["conflict_ids"] = [aliases[ref] for ref in claim["conflict_ids"]]
    return ProjectionResult(model_input={"claims": selected}, diagnostics=ProjectionDiagnostics(excluded=tuple(excluded)),
                            alias_to_claim={alias: original for original, alias in aliases.items()} if alias_identifiers else None)


def project_facts_for_model(
    snapshot: FactSnapshot,
    *,
    private_header: Mapping[str, str | None],
    additional_private_values: tuple[str, ...] = (),
) -> dict:
    """Return only provider data; local omission diagnostics are discarded.

    Header values are used for filtering only. Unknown types, empty or overly
    long facts and detected contact-bearing facts are omitted. This cannot
    discover all names, addresses or obfuscated contact data in free prose.
    """
    return project_facts_with_diagnostics(
        snapshot, private_header=private_header, additional_private_values=additional_private_values
    ).model_input
