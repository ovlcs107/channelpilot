from app.utils.text import split_long_message, strip_html, truncate


def test_strip_html():
    assert strip_html("<b>Hello</b>&nbsp;world") == "Hello world"


def test_truncate():
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"


def test_split_long_message():
    chunks = split_long_message("a" * 5000, limit=1000)
    assert len(chunks) == 5
    assert all(len(chunk) <= 1000 for chunk in chunks)
