# BLS Spain Armenia — Visa Appointment Bot

Мониторит сайт визового центра BLS Spain в Армении и автоматически записывает на приём.

## Быстрый старт

```bash
# 1. Установить зависимости
pip install -r requirements.txt
playwright install chromium

# 2. Скопировать конфиг
cp .env.example .env

# 3. Заполнить .env:
#    - TWOCAPTCHA_API_KEY — ключ от 2captcha.com
#    - Личные данные (имя, паспорт и т.д.)
#    - Telegram-токен (опционально)

# 4. Запустить
python bot.py
```

## Как работает

1. Открывает страницу бронирования через Playwright (Chromium)
2. Заполняет форму: Category, Location, Visa Type, Sub Type, Appointment for
3. Решает hCaptcha через 2captcha.com
4. Проверяет доступные даты
5. Если найдена дата >= `MIN_DATE` — бронирует
6. Уведомляет в Telegram

## Параметры (.env)

| Переменная | Описание |
|---|---|
| `TWOCAPTCHA_API_KEY` | API-ключ 2captcha.com |
| `TARGET_URL` | URL страницы бронирования |
| `CATEGORY` | Normal / Prime Time |
| `LOCATION` | Yerevan |
| `VISA_TYPE` | Schengen Visa |
| `VISA_SUB_TYPE` | Tourism (Short Term) |
| `MIN_DATE` | Минимальная дата (YYYY-MM-DD) |
| `CHECK_INTERVAL` | Интервал проверки (секунды) |
| `HEADLESS` | true/false — показывать браузер |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | ID чата для уведомлений |

## Отладка

- Скриншоты сохраняются в `screenshots/`
- Логи пишутся в `bot.log` и консоль
- На первой итерации бот выводит структуру формы (все select/input элементы)
