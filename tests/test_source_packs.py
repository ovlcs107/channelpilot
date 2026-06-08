from app.services.source_packs import SourcePackRegistry


def test_source_packs_have_ru_and_world() -> None:
    registry = SourcePackRegistry("data/source_packs.json")
    keys = {pack.key for pack in registry.list_packs()}
    assert "ru_general" in keys
    assert "world_general" in keys
    assert "ru_world" in keys


def test_source_pack_items_have_domains() -> None:
    registry = SourcePackRegistry("data/source_packs.json")
    pack = registry.require("world_general")
    assert len(pack.sources) >= 3
    assert all(item.url.startswith("https://") for item in pack.sources)
    assert all(item.domain for item in pack.sources)
