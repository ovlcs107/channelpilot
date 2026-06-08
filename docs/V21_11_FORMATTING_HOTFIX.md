# v21.11 Formatting Hotfix

This hotfix improves Telegram channel post readability.

## Changes

- Added `app/services/post_formatter.py`.
- Added automatic post layout normalization before Telegram parse-mode conversion.
- First line is treated as a heading when safe and rendered as bold markdown-ish text.
- Long paragraphs are split into shorter blocks without changing facts.
- CTA footer is forced onto a separate paragraph.
- AI formatting instructions now require a strict Telegram layout with whitespace.
- Existing DB init fixes, `/start` plain text behavior and draft confirmation fixes are preserved.

## Verification

Executed:

```bash
python -m compileall -q app tests
pytest -q tests/test_v21_11_post_formatter.py
```

Result for new formatter tests: `2 passed`.

Full pytest may require project dependencies such as SQLAlchemy/aiogram in the current environment.
