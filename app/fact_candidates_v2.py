"""Deterministic, unconfirmed candidate spans from extracted material.

This helper never calls a provider, makes semantic classifications or stores
data. Offsets are Unicode code points in the returned normalized source text.
"""
from __future__ import annotations

from app.content_v2 import normalize_material_text, text_hash

MAX_MATERIAL = 20_000
MAX_CANDIDATES = 200


def extract_candidates(text: str) -> dict:
    if not isinstance(text, str):
        raise ValueError("素材格式无效")
    source = normalize_material_text(text)
    if len(source) > MAX_MATERIAL:
        raise ValueError("素材超过 20000 字，请分段整理")
    candidates = []
    offset = 0
    for index, line in enumerate(source.splitlines(keepends=True)):
        content = line.rstrip("\n")
        stripped = content.strip()
        if stripped:
            leading = len(content) - len(content.lstrip())
            start = offset + leading
            end = start + len(stripped)
            candidates.append({"temp_key": f"candidate-{index + 1}", "fact_type": "unclassified",
                               "value": source[start:end], "start": start, "end": end,
                               "quote_hash": text_hash(source[start:end]), "confirmation": "unconfirmed",
                               "completion": "unknown"})
            if len(candidates) > MAX_CANDIDATES:
                raise ValueError("素材行数过多，请先合并相关内容")
        offset += len(line)
    return {"normalized_text": source, "content_hash": text_hash(source), "candidates": candidates}
