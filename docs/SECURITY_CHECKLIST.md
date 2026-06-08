# Security Checklist — ChannelPilot AI v20

Перед клиентским или приватным запуском проверьте:

- `BOT_ACCESS_MODE=private`
- `ADMIN_IDS` задан корректно.
- `ALLOWED_CHANNELS` содержит только разрешённые каналы.
- `PUBLIC_ALLOW_AUTO_PUBLISH=false`
- `PUBLIC_DOUBLE_CONFIRM_PUBLISH=true`
- `PUBLIC_REQUIRE_USER_CHANNEL_ADMIN=true`
- `REQUIRE_CHANNEL_VERIFICATION=true`
- Старые деплои с тем же bot token остановлены.
- Бот добавлен в канал только с нужными правами.
- Канал прошёл `/check`.
- Для тестового клиента включён `DEMO_MODE=true` и лимиты.
- В логах нет секретов: bot token, AI key, DATABASE_URL password.

v20 не публикует через Natural Control напрямую: AI-router только определяет намерение, а backend проверяет права и создаёт черновик.
