from app.services.launch_kit import commercial_offer_text, demo_script_text, launch_checklist_text, pricing_text, setup_master_text


def test_launch_checklist_contains_core_commands():
    text = launch_checklist_text()
    assert "/doctor" in text
    assert "/check @client_news" in text
    assert "/usage" in text
    assert "/audit" in text


def test_demo_script_is_client_safe():
    text = demo_script_text()
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "AI_API_KEY" not in text
    assert "исходник" not in text.lower()


def test_offer_and_pricing_have_neutral_examples():
    offer = commercial_offer_text()
    pricing = pricing_text()
    assert "ChannelPilot AI" in offer
    assert "Тестовый запуск" in pricing
    assert "@tgmedialab" not in offer.lower()


def test_setup_master_includes_channels():
    text = setup_master_text(["@client_news", "@world_digest"])
    assert "@client_news" in text
    assert "@world_digest" in text
