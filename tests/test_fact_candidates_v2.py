import pytest

from app.content_v2 import text_hash
from app.fact_candidates_v2 import extract_candidates


def test_candidates_point_to_exact_normalized_unicode_source():
    result = extract_candidates("  机械课程 😀\r\n\r\n  整理记录  \r下一行")
    source = result["normalized_text"]
    assert source == "  机械课程 😀\n\n  整理记录  \n下一行"
    assert result["content_hash"] == text_hash(source)
    assert len(result["candidates"]) == 3
    for candidate in result["candidates"]:
        excerpt = source[candidate["start"]:candidate["end"]]
        assert candidate["value"] == excerpt
        assert candidate["quote_hash"] == text_hash(excerpt)
        assert candidate["confirmation"] == "unconfirmed"
        assert candidate["completion"] == "unknown"
        assert candidate["fact_type"] == "unclassified"


def test_empty_and_unbounded_input():
    assert extract_candidates("\r\n  \n")["candidates"] == []
    with pytest.raises(ValueError):
        extract_candidates("a" * 20_001)
    with pytest.raises(ValueError):
        extract_candidates("x\n" * 201)
