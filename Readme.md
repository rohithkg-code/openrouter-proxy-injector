<p align="center">

    <b>Openrouter-Proxy-Injector:</b> Openrouter API Keys management for heavy usage cases.<br />

    Smart proxy server for OpenRouter key rotation with automated mitigation of upstream server rate limits

</p>

# Readme on other languages

[![en](https://img.shields.io/badge/lang-en-blue.svg)](https://github.com/serjs/openrouter-proxy-injector/blob/oss/Readme.md)
[![ru](https://img.shields.io/badge/lang-ru-red.svg)](https://github.com/serjs/openrouter-proxy-injector/blob/oss/docs/Readme-ru.md)

# Service overview

Openrouter proxy injector enables you to launch and manage Openrouter API keys for both DEV and PRODUCTION environments, while also tracking and handling rate limits from upstream providers for both paid and free models.

It’s ideal for Vibe coding, intensive AI agent usage, or simply developing with the Openrouter API.

# Features

- Use different billing API keys for your agent swarm

- Manage request limits of 50 requests per day for each API account, enabling unlimited use of Free models through multiple accounts

- Automatically retry requests until a response is received from the upstream model when hitting upstream service limits (e.g., frequent Google Gemini 429 rate limits during intensive agent usage)

- Support all openrouter API methods for model interaction as is

- Handling streaming and non-streaming requests with 429 Rate limit handling

- Respects Openrouter API retry limit per minute for retrying

# Tech details

<video controls width="600">
<source src="./docs/howitworks_animation.mp4" type="video/mp4">
Your browser does not support the video tag
</video>

<details>
  <summary>Technical architecture</summary>
  <p>

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

  </p>
</details>

# App config parameters

|ENV variable|Type|Required|Default|Description|
|------------|----|--------|-------|-----------|
|PROXY_API_KEY|String|True|EMPTY|You custom unified API key for handling your requests|
|OPENROUTER_KEYS|String|True|EMPTY|Openrouter API Keys. For e.g. `OPENROUTER_KEYS=sk...ab,sk...cd` and so on|
|TIMEZONE|String|False|UTC|Timezone for handling daily API usage limits for free models. Used to reset limited and locked keys.|
|UVICORN_PORT|Int|False|9999|Default app port to listen|
|UVICORN_HOST|Int|False|0.0.0.0|Default app ip to listen|
|UVICORN_LOG_LEVEL|String|False|info|Set logging level. E.g. debug for show requests details, including body and responses. OpenRouter API Keys are obfuscated in debug logs.

# Quickstart with Docker

1. [Install](https://docs.docker.com/engine/install/) Docker engine

2. Run container `docker run -it -e PROXY_API_KEY=<RANDOM_UNIQE_STRONG_KEY> -e OPENROUTER_KEYS=sk...ab,sk...cd -p 9999:9999 ghcr.io/serjs/openrouter-proxy-injector:latest`

3. Set OpenRouter API URL as your docker host URL, e.g. http://<docker_host_ip>:9999

4. That it

5. Check Openrouter Proxy Injector API and key statuses

  [http://<docker_host_ip>:9999/docs](http://<docker_host_ip>:9999/docs)

  [http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get](http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get)

# Quickstart wirh docker-compose

1. Copy and edit .env `cp .end.sample .env`

   Fill `PROXY_API_KEY` and `OPENROUTER_KEYS` as minimal required paramters

2. Run `docker compose up -d`

4. That it

5. Check Openrouter Proxy Injector API and key statuses

  [http://<docker_host_ip>:9999/docs](http://<docker_host_ip>:9999/docs)

  [http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get](http://<docker_host_ip>:9999/docs#/default/get_key_status_key_status_get)

# Examples for diffrent products

## VSCode Continue

## LobeChat

## Custom Agentic Code

## AnythingLLM
