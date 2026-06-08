# ChannelPilot AI v21.2 — Autopilot Diagnostics

## v21: Client Launch Kit

В этой сборке добавлены инструменты для безопасного запуска, демонстрации и продажи решения:

- расширенное главное меню `/menu`;
- мастер первичной настройки `/setup`;
- чеклист перед запуском `/launchcheck`;
- сценарий демонстрации `/demo`;
- короткое коммерческое описание `/offer`;
- ориентиры по цене `/pricing`;
- документы `docs/DEMO_RUNBOOK.md`, `docs/QA_CHECKLIST.md`, `docs/COMMERCIAL_ONE_PAGER.md`, `docs/PRICING_GUIDE.md`, `docs/BUG_REPORT_TEMPLATE.md`.

Команды не заменяют основную логику бота, а помогают быстро подготовить демо, проверить деплой и не забыть критичные пункты безопасности.


Закрытая production-сборка Telegram AI-SMM бота для приватного использования, клиентского внедрения и работы с сетью Telegram-каналов.

ChannelPilot AI помогает безопасно готовить контент: новости, дайджесты, редакционные посты, материалы по промпту канала, медиа, расписание и публикации через подтверждение. Основной интерфейс — обычное общение с ботом; команды оставлены как резервный технический режим.

## Что нового в v20

- Natural AI Control: задачи можно писать обычным языком.
- Улучшенная защита доступа: `ADMIN_IDS`, `ALLOWED_CHANNELS`, проверка прав пользователя и бота.
- Роли: owner / operator / viewer.
- Audit log: кто создал, изменил, запланировал или опубликовал материал.
- Usage / limits: контроль AI-запросов и оценочных токенов.
- Demo mode для безопасного тестового доступа клиенту.
- Улучшенная проверка медиа: сомнительные фото/логотипы/рандомные превью не прикрепляются.
- Anti-cringe filter: убирает типичные нейросеточные клише.
- Content day: быстрый план публикаций на день.
- Backup/export настроек канала.
- Расширенные пресеты: `classic_news`, `meme_news`, `serious_media`, `gaming`, `tech_ai`, `business`, `expert_blog`, `author_blog`.
- Чистый CTA-footer: без голых источников в канале, например `Подписаться на TG Media Lab`.

## Безопасная публикация

1. Пользователь пишет задачу или использует команду.
2. AI-router определяет intent, но не публикует сам.
3. Backend проверяет канал, роль, whitelist, verified-статус, права пользователя и права бота.
4. Создаётся черновик.
5. Публикация выполняется только после финального подтверждения кнопкой.
6. Действие записывается в audit log.

## Примеры обычного общения

```text
Сделай новость для @client_news про Telegram Ads
Сделай дайджест для @world_digest по мировым новостям
Поставь для @brand_media пресет serious_media
Подготовь день контента для @client_news на завтра
Покажи расходы
Покажи audit @client_news
Экспортируй настройки @client_news
```

## Главные команды

```text
/check @channel
/prompt @channel | редакционная политика канала
/news @channel тема
/digest @channel тема
/promptpost @channel | тема
/preset @channel classic_news|meme_news|serious_media|gaming|tech_ai|business|expert_blog|author_blog
/day @channel
/usage
/limits
/audit [@channel]
/export @channel
/roles
/addoperator telegram_id
/addviewer telegram_id
/myrole
```

## Railway variables минимум

```env
TELEGRAM_BOT_TOKEN=токен_от_BotFather
DATABASE_URL=${{Postgres.DATABASE_URL}}
AI_API_KEY=ключ_OpenRouter_или_OpenAI
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openai/gpt-4o-mini
AI_FAST_MODEL=openai/gpt-4o-mini

BOT_ACCESS_MODE=private
ADMIN_IDS=123456789
ALLOWED_CHANNELS=@client_news,@brand_media
REQUIRE_CHANNEL_VERIFICATION=true
PUBLIC_REQUIRE_USER_CHANNEL_ADMIN=true
PUBLIC_DOUBLE_CONFIRM_PUBLISH=true
PUBLIC_ALLOW_AUTO_PUBLISH=false

NATURAL_CONTROL_ENABLED=true
NATURAL_CONTROL_USE_AI=true
BOT_DM_PARSE_MODE=markdown_v2
POST_FORMAT_DEFAULT=markdown_v2

SOURCE_FOOTER_MODE=brand_cta
BRAND_CTA_PREFIX=Подписаться на
BRAND_CTA_LABEL=TG Media Lab
BRAND_CTA_URL=https://t.me/TMediaLabG
```

## Экономия AI

Можно задать разные модели под разные задачи:

```env
AI_ROUTER_MODEL=google/gemini-2.5-flash-lite
AI_DRAFT_MODEL=google/gemini-2.5-flash-lite
AI_REWRITE_MODEL=google/gemini-2.5-flash-lite
AI_REVIEW_MODEL=google/gemini-2.5-flash-lite
AI_QUALITY_MODEL=openai/gpt-4o-mini
```

Если переменные не заданы, используется `AI_MODEL` / `AI_FAST_MODEL`.

## Demo mode

Для тестового клиента:

```env
DEMO_MODE=true
DEMO_MAX_POSTS_PER_DAY=5
DEMO_MAX_CHANNELS=1
DEMO_REQUIRE_CONFIRMATION=true
```

Auto-publish по умолчанию выключен.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m app.main
```

## Проверки

```bash
python -m compileall app tests
pytest -q
```


### Topic relevance guard

Новостной рейтинг теперь учитывает не только свежесть и качество источника, но и соответствие теме запроса/канала. Это защищает от ситуации, когда для канала про Telegram, AI и SMM попадает свежая, но нерелевантная политическая или автомобильная новость.

Настройки:

```env
TOPIC_RELEVANCE_ENABLED=true
TOPIC_RELEVANCE_MIN_SCORE=35
TOPIC_RELEVANCE_STRICT_MIN_SCORE=45
```

Для каналов про Telegram, AI, SMM, медиа и технологии включается более строгая фильтрация политического шума, если политика не связана напрямую с темой канала.

## Autopilot diagnostics

If the scheduler logs show `_run_autopilot executed successfully` but no drafts appear, use:

```text
/autostatus @client_news
/autorun @client_news
/sourcecheck @client_news topic
```

Autopilot now accepts a tolerance window through:

```env
AUTOPILOT_SLOT_WINDOW_MINUTES=5
```

This prevents Railway delays from missing the exact minute. `/autopilot @channel on` also switches `safe` mode to `semi` automatically so the bot can prepare drafts without publishing automatically.
