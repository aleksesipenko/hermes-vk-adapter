# Hermes VK Adapter

Hermes VK Adapter — это плагин платформы для Hermes Agent, который подключает
Hermes к обычным сообщениям VK-сообщества через VK Bots Long Poll.

Идея простая: пользователь пишет в личные сообщения VK-сообщества или в VK
беседу, плагин принимает событие через Long Poll, передаёт его в Hermes Gateway,
а ответ Hermes отправляется обратно через VK `messages.send`.

```text
VK community messages
-> VK Bots Long Poll
-> Hermes plugin: vk
-> Hermes Gateway
-> Hermes Agent
-> VK messages.send / document upload
```

Это не personal-account userbot, не VK Teams, не MCP tool и не отдельный Node
sidecar. Нужен именно токен VK-сообщества.

## Зачем

Плагин нужен, чтобы использовать Hermes как личного VK-бота без личного токена
VK-аккаунта и без отдельного процесса-посредника.

Что уже поддерживается:

- личные сообщения VK-сообщества;
- VK беседы с политикой mention/reply для обычного текста;
- slash-команды Hermes, например `/status`, `/commands`, `/model`;
- callback-кнопки для model picker, approvals и clarify — только когда
  клиент их поддерживает и автора запроса можно доказать (личка или
  per-user групповая сессия Hermes), с TTL и защитой от повторов;
- текстовый fallback на штатные команды Hermes (`/approve`, `/deny`,
  `/model`) для остальных случаев;
- allowlist по numeric VK user id; без явного `VK_ALLOWED_USERS` или
  `VK_ALLOW_ALL_USERS=true` адаптер не подтверждает входящие (нет отметки
  о прочтении и реакций);
- plain-text вывод без Telegram Markdown assumptions, с разбиением длинных
  ответов без потери символов;
- входящие фото, документы и voice/audio message как Hermes media;
- reply/forward-контекст и метаданные неподдерживаемых вложений как текст,
  с общими лимитами на глубину, число сообщений, число вложений и объём;
- нативная отправка JPG/JPEG, PNG и GIF как VK-фото, батчами;
- отправка сгенерированных документов через VK docs upload;
- `messages.edit` и `messages.delete`, с продолжением при лимите 4096;
- реальные имена пользователей и названия бесед с кэшем и TTL;
- опциональные числовые VK-реакции и отметка о прочтении;
- `VK_HOME_PEER_ID` для cron/proactive delivery;
- `hermes vk-doctor` для локальной и read-only живой диагностики.

Чего плагин не делает:

- не восстанавливает пропущенные сообщения после долгого простоя gateway:
  дедупликация Long Poll держится в памяти процесса, персистентного backlog
  нет;
- не рассчитан на публичный многопользовательский режим: держи
  `VK_ALLOW_ALL_USERS=false` и заполняй `VK_ALLOWED_USERS`;
- не использует Callback API/webhook, только Bots Long Poll;
- не поддерживает personal/user-токены, VK Mini Apps, стену, рассылки и
  carousel;
- не отправляет голосовые сообщения нативно: аудио уходит документом;
- не привязывает callback-кнопки к автору в общей (shared) групповой сессии:
  в этом случае используется текстовый fallback, а не кнопки;
- не знает, упал ли сам агент: `VK_REACTION_FAILED_ID` отражает только
  неудачную отправку самим адаптером;
- ставит реакции только на то сообщение, на которое реально отвечает: если
  Hermes не передал reply-якорь (например, cron/proactive), реакции нет;
- не показывает свой picker моделей на клиентах без callback-кнопок: там
  работает штатный текстовый вывод `/model` из Hermes со списком моделей;
- не публикуется как wheel: Hermes ставит плагин как каталог.

Проверено тестами на моках VK; живые VK-вызовы в CI не выполняются.

## Установка

Официальная установка через Hermes CLI:

```bash
hermes plugins install aleksesipenko/hermes-vk-adapter --enable
```

Автоматическая установка одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/aleksesipenko/hermes-vk-adapter/main/scripts/install.sh | bash
```

Bootstrap-скрипт использует официальный Hermes CLI. Он проверяет `hermes` и
`git`, ставит `httpx` в Python-окружение, из которого запускается Hermes, затем
выполняет `hermes plugins install ... --enable`.

`httpx` — единственная runtime-зависимость плагина. Весь VK-трафик идёт через
один raw HTTP-клиент: отдельный VK SDK не нужен и не используется.

Во время установки Hermes CLI спросит обязательные значения из `plugin.yaml` и
сохранит их в `~/.hermes/.env`:

- `VK_GROUP_TOKEN`: токен VK-сообщества, не личный пользовательский токен;
- `VK_GROUP_ID`: положительный numeric id VK-сообщества, без минуса.

После установки проверь:

```bash
hermes plugins list
hermes gateway status
```

Если менял `.env` или обновлял плагин:

```bash
hermes gateway restart
```

## Настройка VK

Сначала настрой VK-сообщество, потом запускай Hermes Gateway.

### 1. Создать закрытое VK-сообщество

1. Открой VK -> `Сообщества` -> `Создать сообщество`.
2. Выбери `Группа`.
3. Назови группу, например `Hermes Agent`.
4. В настройках сделай группу закрытой или приватной настолько, насколько
   позволяет VK UI.
5. Отключи лишние публичные поверхности: стену, комментарии, обсуждения,
   товары и всё, что не нужно для личного бота.

Визуально это будет личный бот, но технически это диалог с VK-сообществом.

### 2. Включить сообщения сообщества

1. Открой созданное сообщество.
2. Перейди в `Управление` -> `Сообщения`.
3. Включи `Сообщения сообщества`.
4. Если VK показывает блок `Настройки для бота`, включи bot/chatbot features.
5. Если Hermes должен работать в групповых беседах, включи разрешение добавлять
   сообщество в беседы.

### 3. Создать ключ доступа сообщества

Ключ нужно создавать именно на странице того VK-сообщества, через которое будет
работать Hermes. Не создавай его в настройках личного профиля, VK ID,
Standalone-приложения или стороннего приложения.

1. Открой нужное VK-сообщество.
2. Перейди в `Управление` -> `Настройки` -> `Работа с API` -> `Ключи доступа`.
3. Нажми `Создать ключ`.
4. Выдай рекомендуемые права доступа:
   - `manage` / управление сообществом: нужно для `groups.getLongPollServer`,
     иначе VK не отдаст данные Bots Long Poll;
   - `messages` / сообщения сообщества: нужно для входящих/исходящих сообщений,
     typing status и callback-кнопок;
   - `docs` / документы сообщества: нужно для отправки сгенерированных файлов
     через `docs.getMessagesUploadServer` и `docs.save`, а также как fallback
     для картинок, которые VK не принимает как фото;
   - `photos` / фотографии сообщества: нужно для нативной отправки изображений
     через `photos.getMessagesUploadServer` и `photos.saveMessagesPhoto`.
5. Без `photos` Hermes сможет отправлять текст и документы, но картинки будут
   уходить только через document fallback, не как полноценные VK-фото.
6. Не выдавай лишние права вроде `wall`, `market`, `stories`, если они не нужны
   твоему отдельному сценарию.
7. Не выдавай права личного аккаунта и не используй personal/user token.
8. Скопируй токен один раз и храни его как секрет.

В Hermes этот токен должен попасть только в `~/.hermes/.env` как
`VK_GROUP_TOKEN`. Не вставляй его в README, issues, screenshots, коммиты или
публичные чаты.

### 4. Включить Long Poll API

1. Перейди в `Управление` -> `Настройки` -> `Работа с API` -> `Long Poll API`.
2. Включи Long Poll API.
3. Выбери версию API `5.199`, если она доступна. Если VK показывает только
   более новую версию, используй её и задай такое же значение в
   `VK_API_VERSION`.
4. Включи события:
   - `message_new`: входящие сообщения;
   - `message_event`: callback-кнопки, model picker, approvals, clarify;
   - `message_reply`: опционально, полезно для диагностики исходящих ответов.
5. Сохрани настройки.

### 5. Собрать id для Hermes

Нужны такие значения:

```bash
VK_GROUP_ID="<positive numeric community id>"
VK_ALLOWED_USERS="<your numeric VK user id>"
VK_HOME_PEER_ID="<your DM peer_id or group peer_id>"
```

`VK_GROUP_ID` — положительный id сообщества без минуса. Например, если VK
показывает `club123456789`, значение будет `123456789`.

`VK_ALLOWED_USERS` — comma-separated список numeric VK user id, которым
разрешено говорить с Hermes. Для личного использования обычно это один твой id.

`VK_HOME_PEER_ID` нужен для cron/proactive delivery. Для DM с сообществом он
обычно совпадает с numeric VK user id. Для групповой беседы VK peer id обычно
имеет вид `2000000000 + chat_id`. Самый надёжный способ: после установки
отправить первое сообщение сообществу и посмотреть `peer_id` в логах gateway.

### 6. Заполнить Hermes env

`VK_GROUP_TOKEN` и `VK_GROUP_ID` Hermes CLI спросит во время установки.

Остальные значения добавь в Hermes env-файл вручную:

```bash
hermes config env-path
```

Открой показанный файл и добавь:

```bash
VK_ALLOWED_USERS="<your numeric VK user id>"
VK_HOME_PEER_ID="<your DM peer_id or group peer_id>"
VK_API_VERSION="5.199"
VK_ALLOW_ALL_USERS="false"
VK_REQUIRE_MENTION="true"
VK_COMMAND_KEYBOARD="true"
```

После этого:

```bash
hermes gateway restart
hermes gateway status
```

Проверка: напиши в DM сообщества `/status` или `/commands`. Для групповой
беседы slash-команды должны работать без mention, а обычный текст должен
требовать mention, reply или другой activation signal.

## Диагностика: hermes vk-doctor

`hermes plugins list` и `hermes gateway status` показывают только то, что
плагин импортировался и переменные окружения заданы. Этого мало: токен может
быть отозван, Long Poll выключен, а gateway всё равно покажет «connected».

```bash
hermes vk-doctor              # только локальные проверки, без сети
hermes vk-doctor --live       # + read-only проверки VK
hermes vk-doctor --live --json
```

Локальный режим проверяет форму конфигурации: токен задан (значение не
печатается), `VK_GROUP_ID` — положительное число, `VK_API_VERSION` вида `5.199`.

Локальный режим также проверяет `VK_HOME_PEER_ID`: заданное, но не числовое
или неположительное значение — ошибка, а не пропуск (cron-доставка включена и
молча уходит в никуда). Отсутствующее значение остаётся `skip`.

`--live` дополнительно делает только читающие вызовы:

- `groups.getTokenPermissions` — есть ли `messages`, `manage`, `docs`, `photos`;
  лишние права показываются как предупреждение (least privilege);
- `groups.getLongPollSettings` — включён ли Long Poll, есть ли события
  `message_new` и `message_event`, совпадает ли версия с `VK_API_VERSION`;
- `groups.getLongPollServer` — доступность;
- `messages.getConversationsById` — виден ли `VK_HOME_PEER_ID`.

Ничего не отправляется и не изменяется. Отправка возможна только явно:

```bash
hermes vk-doctor --live --send-smoke --peer-id <peer>
```

Без `--peer-id` команда откажется угадывать адресата.

Коды выхода: `0` — всё в порядке, `1` — есть предупреждения, `2` — есть ошибки.
Токен не печатается ни в одном режиме, включая тексты ошибок VK.

## Реакции, отметка о прочтении, edit и delete

Hermes умеет редактировать и удалять свои VK-сообщения через `messages.edit` и
`messages.delete`. У `messages.edit` лимит 4096 символов — если финальный ответ
длиннее, первая часть заменяет исходное сообщение, а остаток уходит следующими
сообщениями, поэтому текст не обрезается.

Входящее сообщение отмечается прочитанным только после того, как локальный
allowlist его принял. Если allowlist не настроен вообще, подтверждений нет:
отсутствие настройки не означает разрешение. Отключить: `VK_DISABLE_MARK_READ="true"`.

Реакции на жизненный цикл включены без настройки: 👌 при начале обработки,
👍 после успешной отправки ответа и 😡 при ошибке отправки. VK использует для
них стандартные числовые id `10`, `4` и `8`.

Реакция «готово»/«ошибка» ставится только на то входящее сообщение, ответом
на которое является отправка. Цель определяется по reply-якорю, который
Hermes передаёт в `send()`. Если якоря нет (cron, proactive-доставка) или он
неизвестен, реакция не ставится вообще — вместо того чтобы отметить чужое или
устаревшее сообщение. Реакции остаются косметикой: ошибка VK при их установке
не влияет на доставку ответа.

## Поведение в VK беседах

В групповых беседах действует раздельная политика:

- slash-команды, например `/status`, `/commands`, `/model`, работают без
  mention;
- обычный текст не уходит в LLM/agent без mention, reply-to-bot или другого
  activation signal;
- это защищает беседу от ситуации, где Hermes отвечает на каждую реплику.

## Локальная разработка

Локальная разработка идёт только внутри этого репозитория. Не ставь зависимости
этого проекта в Mac-wide Hermes runtime.

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Для локальной проверки Hermes внутри repo:

```bash
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git vendor/hermes-agent
. .venv/bin/activate
python -m pip install -e vendor/hermes-agent
HERMES_HOME="$PWD/.hermes-local" .venv/bin/hermes --help
```

Установить локальную копию плагина в repo-local Hermes home:

```bash
HERMES_HOME="$PWD/.hermes-local" .venv/bin/hermes plugins install "$PWD" --enable --force
```

Проверки:

```bash
. .venv/bin/activate
HERMES_HOME="$PWD/.hermes-local" python -m pytest -q
python -m ruff check . --exclude vendor --exclude .venv --exclude .hermes-local
git diff --check
```

## Структура

```text
.
├── plugin.yaml              # root manifest for hermes plugins install
├── __init__.py              # root register() shim
├── after-install.md         # Hermes post-install message
├── plugins/vk/              # adapter implementation
├── scripts/install.sh       # one-command bootstrap
├── config/.env.example
└── tests/
```

## Безопасность

- Используй только VK community access token.
- Не используй personal VK user token.
- Не коммить `VK_GROUP_TOKEN`, provider keys, private auth files или реальные
  `.env`.
- Для приватного личного бота держи `VK_ALLOW_ALL_USERS=false` и заполняй
  `VK_ALLOWED_USERS`.
