# Hermes VK Adapter установлен

Платформенный плагин VK установлен и включён.

Перед запуском gateway прочитай инструкцию по настройке:

https://github.com/aleksesipenko/hermes-vk-adapter#readme

Следующие проверки:

```bash
hermes plugins list
hermes gateway status
```

Чеклист настройки VK:

1. Используй токен VK-сообщества, не личный токен VK-пользователя.
2. Ключ создавай в самом сообществе: `Управление` -> `Настройки` -> `Работа с API` -> `Ключи доступа`.
3. Выдай ключу права `messages`, `manage`, `docs`, `photos`.
4. Включи сообщения сообщества.
5. Включи события Long Poll API: `message_new` и `message_event`.
6. Включи функции бота / чат-бота в настройках VK-сообщества.
7. Заполни `VK_ALLOWED_USERS`: числовые VK user id пользователей, которым можно писать Hermes.
8. Заполни `VK_HOME_PEER_ID` после первого Long Poll сообщения, когда в логах gateway появится настоящий peer id.

После изменения настроек плагина или окружения перезапусти gateway:

```bash
hermes gateway restart
```
