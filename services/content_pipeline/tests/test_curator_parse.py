"""Curator JSON parsing — tolerate fenced output and chatter."""

from app.services.curator import _parse_score


def test_parse_plain_json():
    score, reason = _parse_score('{"score": 0.82, "reason": "great hook"}')
    assert score == 0.82
    assert reason == "great hook"


def test_parse_clamps_score_to_unit_interval():
    score, _ = _parse_score('{"score": 1.5, "reason": "x"}')
    assert score == 1.0
    score, _ = _parse_score('{"score": -0.2, "reason": "x"}')
    assert score == 0.0


def test_parse_extracts_from_chatter_prefix():
    text = "Sure: " + '{"score": 0.5, "reason": "ok"}' + "\nthat's my call."
    score, reason = _parse_score(text)
    assert score == 0.5


def test_parse_returns_none_for_garbage():
    score, reason = _parse_score("not json at all")
    assert score is None
    assert reason == "parse_failed"


def test_parse_returns_none_when_score_missing():
    score, reason = _parse_score('{"reason": "no score"}')
    assert score is None
    assert reason == "missing_score"
