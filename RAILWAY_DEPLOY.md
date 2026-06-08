# Railway deploy — ChannelPilot AI v20

## 1. Создайте сервисы

1. Railway project.
2. PostgreSQL service.
3. Bot service from GitHub repository.

## 2. Variables в bot service

```env
TELEGRAM_BOT_TOKEN=...
DATABASE_URL=${{Postgres.DATABASE_URL}}
AI_API_KEY=...
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
POST_FORMAT_DEFAULT=html
SOURCE_FOOTER_MODE=brand_cta
BRAND_CTA_PREFIX=Подписаться на
BRAND_CTA_LABEL=TG Media Lab
BRAND_CTA_URL=https://t.me/TMediaLabG
```

Если PostgreSQL service называется не `Postgres`, замените `Postgres` на имя своего сервиса.

## 3. Healthcheck

Это worker-бот на long polling. Обязательный Railway healthcheck лучше не включать. В проекте есть health server на `$PORT`, но деплой не должен зависеть от external HTTP route.

## 4. После деплоя

1. Остановите старые сервисы/локальные процессы с тем же BotFather token.
2. Напишите боту `/start`.
3. Выполните `/doctor`.
4. Добавьте бота админом канала с правом публикации.
5. Выполните `/check @client_news`.
6. Настройте стиль: `/preset @client_news classic_news`.
7. Задайте промпт: `/prompt @client_news | ...`.
8. Создайте тестовый черновик: `/news @client_news тема`.

## 5. Частые проблемы

### Conflict: terminated by other getUpdates request
Запущен второй экземпляр бота с тем же токеном. Оставьте только один active deployment.

### Нет доступа к рабочему пространству
Проверьте `ADMIN_IDS`. Допустимые форматы:

```env
ADMIN_IDS=123456789
ADMIN_IDS=123456789,987654321
ADMIN_IDS=[123456789,987654321]
```

### Канал не разрешён
Проверьте `ALLOWED_CHANNELS`:

```env
ALLOWED_CHANNELS=@client_news,@brand_media
ALLOWED_CHANNELS=["@client_news","@brand_media"]
```

### Пост без медиа
v20 специально не прикрепляет сомнительные медиа. Лучше пост без фото, чем фото не по теме.


## v21 launch check

После деплоя выполните в личке бота:

```text
/doctor
/setup
/launchcheck
```

Перед показом клиенту также пройдите `docs/QA_CHECKLIST.md` и используйте `/demo` как сценарий демонстрации.

## Autopilot on Railway

Railway workers can execute scheduled jobs a little later than the exact minute. Set:

```env
AUTOPILOT_SLOT_WINDOW_MINUTES=5
```

To diagnose autopilot:

```text
/autostatus @client_news
/autorun @client_news
```

If `/autostatus` shows no active slot, update `/times @client_news 09:00,14:00,19:00` using the timezone in `APP_TIMEZONE`.
