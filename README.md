# GOAT Gifts AI

Русскоязычный Telegram-бот и Mini App для анализа коллекционных подарков. Приложение ведёт учёт подписок и платежей в Stars, показывает расчётные возможности и хранит личный и виртуальный портфели. Покупку и продажу подарков пользователь выполняет на площадке самостоятельно.

## Локальный запуск

Требуется Docker с Compose. Скопируйте параметры по умолчанию и запустите сервис.

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f app
```

Терминал доступен на [localhost:8000](http://localhost:8000/). Для проверки PostgreSQL и запущенного приложения откройте [health](http://localhost:8000/health). В локальной конфигурации без токена работают публичное синтетическое демо, состояние площадок и калькулятор. Для сохранения портфеля откройте Mini App через Telegram с настроенным ботом и публичным HTTPS-адресом.

### Запуск из исходников

```bash
cp .env.example .env
docker compose up -d db
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python start.py
```

Для разработки интерфейса можно запустить `npm --prefix frontend run dev` параллельно с FastAPI. Vite проксирует API на порт 8000.

### Проверки

```bash
TEST_DATABASE_URL=postgresql://goat:goat@localhost:5432/goat .venv/bin/pytest -q
npm --prefix frontend run build
```

Каждый тест PostgreSQL создаёт и удаляет свою схему. У тестовой роли должно быть право создавать схемы. Используйте выделенную тестовую базу. Тестовые Stars и идентификаторы не отправляются во внешние сервисы.

## Структура

```text
goat-gifts-ai/
  start.py                 FastAPI, Telegram, миграции и фоновые задачи
  goat/
    accounts.py            Telegram initData, пользователи, права, аудит
    api.py                 Mini App API и раздача frontend
    billing.py             заказы, платежи, возвраты, промокоды, статистика
    bot.py                 пользовательский и административный Telegram-интерфейс
    config.py              переменные окружения
    db.py                  подключение PostgreSQL и блокировки
    finance.py             расчёт расходов, выручки, ROI и сценариев
    jobs.py                PostgreSQL-очередь, рассылки и уведомления
    market.py              Portals, интерфейс площадок и торговый движок
    telegram.py            long polling и сохранение позиции обработки
    terminal.py            демо, фильтры и операции портфеля
  frontend/                React, TypeScript, Vite, Tailwind, Motion, Lightweight Charts
  migrations/              схемы Alembic
  tests/                   тесты на синтетических данных
  scripts/backup.sh        экспорт PostgreSQL
  docs/                    развёртывание, управление и проверка интеграций
  Dockerfile
  docker-compose.yml
  requirements.txt
  requirements-dev.txt
  .env.example
```

- [Развёртывание через GitHub и Bothost](docs/bothost.md)
- [Управление GOAT Gifts AI](docs/operations.md)
- [Источники рыночных данных и условия доступа](docs/integrations.md)
- [Проверки и оставшиеся ограничения](docs/verification.md)

## Готовность

В репозитории реализованы локально проверяемые компоненты подписок, Stars, поддержки, промокодов, рассылок, администрирования, терминала, финансовых расчётов и рыночного движка. Для запуска коммерческой эксплуатации нужны учётные записи Telegram и Bothost, PostgreSQL и проверка реальных платежей. Для работы платной рыночной ленты отдельно нужны подтверждённые права на использование данных и доступ к API площадок. Демо работает независимо от этих интеграций.
