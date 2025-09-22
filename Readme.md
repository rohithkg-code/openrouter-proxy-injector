# OpenRouter Key Proxy

Промежуточный прокси-сервер для ротации ключей OpenRouter с автоматическим обходом ограничений частоты запросов.

Помогает справится с раздражающими ситуациями когда при выполнении Agentic задач мы получаем от Openrouter upstream 429 Rate limit ломающие наши Agent flow или просто отнимающие время.

## Техническая реализация

Схема
```mermaid
graph TD
    A["Client (VSCode / AnythingLLM / Custom Code)"] -->|"Request with APIKEY variable"| B["Proxy Server"]
    B --> C{"Are there available keys?"}
    C -->|"Yes"| D["Select active key"]
    C -->|"No"| E["429 - All keys exhausted"] --> F["Response to client"]
    D --> G["Send request with real OpenRouter key"]
    G --> H{"Response from OpenRouter"}
    H -->|"200 OK"| I["Successful response"] --> F
    H -->|"429 - Provider error (retryable)"| L["Retry request (up to 15 times)"] --> H
    H -->|"429 - Rate limit for free models"| J["Block key until 03:00 UTC"] --> K["Retry with new key if available"] --> C
    H -->|"4XX / 5XX - Other error"| ERR["Return error to client"] --> F
```

## Основные сценарии использования
- Единый сервер для управления различными учетными записями и API ключами (бесплатные, с 10$ кредитом, платные)
- Использование набора прокси серверов для обращения к Openrouter, при необходимости (функционал в планах на разработку)
- Случайное или последовательное использование API ключей, в зависимости от требований к запросам

## Примеры настройки различных решений

### VSCode Continue

### LobeChat

### Custom Agentic Code

### AnythingLLM

## Запуск и настройка
