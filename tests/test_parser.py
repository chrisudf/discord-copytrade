import pytest
from src.parser.signal_parser import parse_signal


def test_basic_call():
    r = parse_signal("Lotto $$IREN 0DTE \$60 calls $$.68")
    assert r["symbol"] == "IREN"
    assert r["side"] == "CALL"
    assert r["strike"] == 60.0
    assert r["price"] == 0.68
    assert r["expiry"] == "0DTE"


def test_put():
    r = parse_signal("$$alert $$TSLA 1DTE \$250 puts \$1.20")
    assert r["symbol"] == "TSLA"
    assert r["side"] == "PUT"
    assert r["strike"] == 250.0
    assert r["price"] == 1.20


def test_invalid():
    assert parse_signal("hello world") is None
