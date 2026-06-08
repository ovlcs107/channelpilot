from app.services.ai_router import NaturalLanguageRouter
from app.services.telegram_formatting import markdown_to_telegram_v2, normalize_format_mode


class DummyWriter:
    async def _chat(self, *args, **kwargs):
        return ""


class DummyChannel:
    def __init__(self, username, title="", topic=""):
        self.username = username
        self.title = title
        self.topic = topic
        self.is_verified = True
        self.editorial_prompt = ""


def test_markdown_to_telegram_v2_preserves_simple_bold():
    rendered = markdown_to_telegram_v2("**Заголовок**\n\nТекст про @channel.")
    assert "*Заголовок*" in rendered
    assert r"\." in rendered


async def test_router_detects_digest_without_ai():
    router = NaturalLanguageRouter(DummyWriter(), use_ai=False)
    route = await router.route("Сделай дайджест для @client_news про технологиям и медиа", [DummyChannel("@client_news")], owner_telegram_id=1)
    assert route.action == "create_digest"
    assert route.channels == ["@client_news"]
    assert "дайджест" in route.topic.lower()


async def test_router_detects_markdown_format():
    router = NaturalLanguageRouter(DummyWriter(), use_ai=False)
    route = await router.route("Поставь markdown для Client News", [DummyChannel("@client_news", title="Client News")], owner_telegram_id=1)
    assert route.action == "set_format"
    assert route.format_mode == "markdown_v2"
    assert route.channels == ["@client_news"]


def test_normalize_format_markdown_aliases():
    assert normalize_format_mode("markdown") == "markdown_v2"
    assert normalize_format_mode("md") == "markdown_v2"
