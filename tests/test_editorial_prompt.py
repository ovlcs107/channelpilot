from app.models import Channel
from app.services.ai_writer import AIWriter
from app.config import Settings


def test_style_text_includes_editorial_prompt():
    ch = Channel(
        owner_id=1,
        username='@client_news',
        topic='Telegram media',
        style='serious',
        editorial_prompt='Write about Telegram growth, AI automation, and ads without hype.',
        style_profile_json='{}',
    )
    text = AIWriter._style_text(ch)
    assert 'редакционная политика' in text
    assert 'Telegram growth' in text


def test_editorial_prompt_settings_defaults():
    settings = Settings(
        TELEGRAM_BOT_TOKEN='x',
        DATABASE_URL='postgresql+asyncpg://user:pass@localhost:5432/db',
        ADMIN_IDS='1',
    )
    assert settings.editorial_prompt_max_chars >= 1000
    assert settings.prompt_post_max_channels >= 1
