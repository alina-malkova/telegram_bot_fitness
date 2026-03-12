# 🍎 Apple Watch → Telegram Bot: Shortcut Setup

Шорткат читает данные из Apple Health и отправляет их боту в Telegram.
Работает автоматически каждое утро.

---

## Шаг 1: Узнай свои параметры

Тебе понадобятся:
- `TELEGRAM_TOKEN` — токен бота (из BotFather)
- `CHAT_ID` — твой chat ID (напиши боту `/start`, он покажет)

URL для отправки сообщений:
```
https://api.telegram.org/bot<TELEGRAM_TOKEN>/sendMessage
```

---

## Шаг 2: Создай шорткат

Открой приложение **Shortcuts** (Команды) на iPhone.

### Нажми "+" → назови "Health Check-In"

Добавь следующие действия **в этом порядке:**

---

### 📊 Блок 1: Читаем данные из Health

**Действие: "Find Health Samples"** (Найти образцы здоровья)
- Type: Heart Rate
- Starting Date: Start of Today → adjusted by -1 day
- Ending Date: Start of Today
- Sort by: Start Date (Newest First)
- Limit: 1
→ Set variable: `RestingHR`

**Действие: "Find Health Samples"**
- Type: Heart Rate Variability
- Starting Date: Start of Today → adjusted by -1 day
- Ending Date: Start of Today
- Sort by: Start Date (Newest First)
- Limit: 1
→ Set variable: `HRV`

**Действие: "Find Health Samples"**
- Type: Active Energy Burned
- Starting Date: Start of Today → adjusted by -1 day
- Ending Date: Start of Today
- Sort by: Start Date (Newest First)
- Limit: 1
→ Set variable: `ActiveCal`

**Действие: "Find Health Samples"**
- Type: Step Count
- Starting Date: Start of Today → adjusted by -1 day
- Ending Date: Start of Today
- Sort by: Start Date (Newest First)
- Limit: 1
→ Set variable: `Steps`

**Действие: "Find Health Samples"**
- Type: Sleep Analysis
- Starting Date: Start of Today → adjusted by -1 day
- Ending Date: Start of Today
- Sort by: Start Date (Newest First)
- Limit: 1
→ Set variable: `Sleep`

**Действие: "Find Health Samples"**
- Type: Workouts
- Starting Date: Start of Today → adjusted by -1 day
- Ending Date: Start of Today
→ Set variable: `Workouts`

---

### 📝 Блок 2: Формируем текст

**Действие: "Text"**

Вставь:
```
/health
⌚ Apple Watch данные:
- Resting HR: [RestingHR] bpm
- HRV: [HRV] ms
- Active calories: [ActiveCal] kcal
- Steps: [Steps]
- Sleep: [Sleep]
- Workouts: [Workouts]
```

(Вставляй переменные через кнопку "Variable" — тапни на каждое поле)

→ Set variable: `HealthMessage`

---

### 📤 Блок 3: Отправляем боту

**Действие: "Get Contents of URL"** (Получить содержимое URL)

- URL: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/sendMessage`
- Method: POST
- Request Body: JSON
- Add fields:
  - `chat_id` (Text): `YOUR_CHAT_ID`
  - `text` (Text): `HealthMessage` (выбери переменную)

---

## Шаг 3: Настрой автоматизацию

1. Shortcuts → **Automation** (Автоматизация) → "+"
2. **Time of Day** → выбери 7:00 AM (или когда хочешь)
3. **Run Immediately** (без подтверждения)
4. Выбери шорткат "Health Check-In"
5. Готово!

---

## Шаг 4: Проверка

1. Открой шорткат → нажми ▶️ (Play)
2. Должно прийти сообщение боту в Telegram с данными
3. Бот автоматически обработает и добавит к чек-ину

---

## Альтернативный простой вариант

Если не хочешь возиться с переменными, сделай минимальный шорткат:

1. **Find Health Samples** → Active Energy (за вчера)
2. **Find Health Samples** → Step Count (за вчера)  
3. **Text**: "/health Калории: [ActiveEnergy], Шаги: [Steps]"
4. **Get Contents of URL** → POST to Telegram API

Остальные данные (HR, HRV, sleep) можно брать из Oura — они точнее для этих метрик.

---

## Решение проблем

**Шорткат не видит данные Health:**
Settings → Privacy → Health → Shortcuts → включи все разрешения

**Автоматизация не срабатывает:**
Убедись что выбрано "Run Immediately" а не "Ask Before Running"

**Бот не отвечает:**
Проверь что `chat_id` правильный (число, не username)

---

## Какие данные откуда лучше брать

| Метрика | Лучший источник | Почему |
|---------|----------------|--------|
| Sleep score | Oura | Более точный алгоритм сна |
| HRV | Oura (ночной) | Измеряет всю ночь, а не точечно |
| Resting HR | Оба похожи | Apple Watch чуть чаще обновляет |
| Active calories | Apple Watch | Носишь весь день |
| Workouts | Apple Watch | Автоопределение тренировок |
| Steps | Apple Watch | Точнее (всегда на руке) |
| Readiness score | Oura | У Apple Watch нет аналога |
