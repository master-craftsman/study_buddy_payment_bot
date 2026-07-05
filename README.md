# School Notion CRM

Локальная реализация системы учета школы из `системв_учета_в_школе.txt`.

Проект делает две вещи:

1. Создает в Notion 6 связанных баз:
   - `Ученики`
   - `Абонементы`
   - `Занятия`
   - `Участия`
   - `Преподаватели`
   - `Кабинеты`
2. Запускает ежедневные проверки оплат, расписания и конфликтов с Telegram-уведомлениями админу.

## Быстрый старт

1. Создай Notion internal integration и дай ей доступ к странице, внутри которой надо создать систему.
2. Скопируй `.env.example` в `.env` и заполни значения.
3. Создай базы:

```bash
python3 setup_notion_school.py
```

Скрипт напечатает ID созданных баз. Сохрани их в `.env`.

4. Проверь уведомления:

```bash
python3 school_payment_alerts.py
```

## Переменные окружения

Обязательные для создания баз:

```bash
NOTION_TOKEN=secret_xxx
NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Обязательные для уведомлений:

```bash
NOTION_TOKEN=secret_xxx
NOTION_VERSION=2026-03-11
NOTION_QUERY_PATH=data_sources
SUBSCRIPTIONS_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ATTENDANCE_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LESSONS_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_BOT_TOKEN=123456:xxx
TELEGRAM_CHAT_ID=123456789
```

Опционально:

```bash
LOW_REMAINING_THRESHOLD=2
DAYS_BEFORE_END_ALERT=5
TELEGRAM_DRY_RUN=1
```

`TELEGRAM_DRY_RUN=1` полезен для проверки: скрипт прочитает Notion и напечатает текст сообщения, но не отправит его в Telegram.

## Запуск перед рабочим днем через GitHub Actions

Workflow уже добавлен в `.github/workflows/school-payment-alerts.yml`.

GitHub Actions использует UTC для cron-расписания. Ежедневный запуск стоит в 08:27 по Астане (`Asia/Almaty`), что соответствует 03:27 UTC:

```yaml
schedule:
  - cron: "27 3 * * *"
```

Workflow также можно запустить вручную через `Actions -> School payment alerts -> Run workflow`.

### GitHub Secrets

Если используешь repository secrets, открой `Settings -> Secrets and variables -> Actions -> New repository secret` и добавь:

```text
NOTION_TOKEN
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Если используешь GitHub Environment secrets, workflow сейчас привязан к environment с именем `.env`:

```yaml
environment: .env
```

В этом случае секреты нужно добавлять в `Settings -> Environments -> .env -> Environment secrets`.

Важно: значение `NOTION_TOKEN` должно быть только самим токеном, без имени переменной и без кавычек:

```text
ntn_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Неправильно:

```text
NOTION_TOKEN=ntn_xxx
Notion-api-token=ntn_xxx
```

ID баз уже прописаны в workflow как обычные env-переменные:

```text
SUBSCRIPTIONS_DB_ID=8f64b263-4e4a-4fa3-9688-057fddfeeda2
ATTENDANCE_DB_ID=7993972d-0d0c-45af-9c7f-14ac30e55866
LESSONS_DB_ID=0b065389-e98e-465a-b8c7-93ccd9f8a34f
```

Локальный `.env` нужен только для запуска на твоем компьютере. Он добавлен в `.gitignore`, потому что содержит секреты.

## Что нельзя создать через API автоматически

Notion API создает базы, свойства, formulas и relations, но не все UI-настройки доступны программно. После создания баз вручную добавь:

- views календаря в базе `Занятия`;
- dashboard-страницу с linked views;
- database buttons в базе `Участия` для быстрых статусов `Был`, `Сгорело`, `Перенос`, `Отмена без списания`.

Подробный чеклист есть в [NOTION_SETUP.md](NOTION_SETUP.md).
