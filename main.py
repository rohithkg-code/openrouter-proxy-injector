import os
import logging
from typing import List, Dict, Optional, AsyncGenerator
import pendulum
import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
import json

app = FastAPI()

# Настройка логирования
log_level_name = os.getenv("UVICORN_LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level)
logger = logging.getLogger("openrouter-proxy")

# Загрузка конфигурации
PROXY_API_KEY = os.getenv("PROXY_API_KEY")
OPENROUTER_KEYS = [
    k.strip() for k in os.getenv("OPENROUTER_KEYS", "").split(",") if k.strip()
]
TIMEZONE = os.getenv("TIMEZONE", "UTC")

# Инициализация состояния ключей
key_status: Dict[str, Optional[pendulum.DateTime]] = {}


@app.on_event("startup")
def initialize_key_status():
    global key_status
    if not OPENROUTER_KEYS:
        logger.error("No OPENROUTER_KEYS provided! Exiting...")
        exit(1)

    key_status = {key: None for key in OPENROUTER_KEYS}
    logger.info(f"Initialized with {len(OPENROUTER_KEYS)} API keys")


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Очищает конфиденциальные данные из заголовков"""
    sensitive_keys = ["authorization", "apikey", "cookie", "set-cookie"]
    sanitized = {}
    for k, v in headers.items():
        key_lower = k.lower()
        if key_lower in sensitive_keys:
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


def log_request_debug(method: str, url: str, headers: Dict[str, str], body: bytes):
    """Логирует детали запроса в debug режиме"""
    # Очищаем заголовки
    sanitized_headers = sanitize_headers(headers)

    # Парсим тело если это JSON
    body_info = None
    if body:
        try:
            body_info = json.loads(body.decode("utf-8"))
        except:
            body_info = f"Binary data ({len(body)} bytes)"

    # Формируем логируемый объект
    log_data = {
        "method": method,
        "url": url,
        "headers": sanitized_headers,
        "body": body_info,
    }

    logger.debug(
        "Request details:\n%s", json.dumps(log_data, indent=2, ensure_ascii=False)
    )


class KeyManager:
    def get_available_key(self) -> Optional[str]:
        current_time = pendulum.now(TIMEZONE)
        for key, lock_time in key_status.items():
            if lock_time is None or current_time >= lock_time:
                return key
        return None

    def block_key_until_next_day(self, key: str):
        unlock_time = pendulum.tomorrow(TIMEZONE).set(hour=3, minute=0, second=0)
        key_status[key] = unlock_time
        logger.warning(
            f"Key {key[:5]}... blocked until {unlock_time.to_iso8601_string()}"
        )

    def handle_rate_limit_response(self, key: str, response: httpx.Response):
        reset_header = response.headers.get("X-RateLimit-Reset")
        if reset_header:
            try:
                reset_time = pendulum.from_timestamp(
                    int(reset_header) / 1000.0, tz=TIMEZONE
                )
                key_status[key] = reset_time
                logger.warning(
                    f"Key {key[:5]}... blocked by rate limit until {reset_time.to_iso8601_string()}"
                )
                return
            except Exception as e:
                logger.error(f"Error parsing reset time: {e}")
        self.block_key_until_next_day(key)

    def get_key_statuses(self) -> Dict[str, str]:
        current_time = pendulum.now(TIMEZONE)
        statuses = {}
        for key, lock_time in key_status.items():
            if lock_time is None:
                statuses[key] = "active"
            elif current_time >= lock_time:
                statuses[key] = "active"
                key_status[key] = None
            else:
                statuses[key] = f"blocked_until_{lock_time.to_iso8601_string()}"
        return statuses


key_manager = KeyManager()


@app.get("/health")
async def health_endpoint(format: Optional[str] = None):
    if format and format.lower() == "json":
        return JSONResponse(content={"status": "OK"}, status_code=200)
    return Response(content="OK", status_code=200)


@app.get("/key-status")
async def get_key_status(apikey: str = Header(..., alias="APIKEY")):
    if apikey != PROXY_API_KEY:
        logger.warning("Invalid proxy API key for key-status endpoint")
        raise HTTPException(status_code=403, detail="Invalid proxy API key")
    return KeyStatusResponse(keys=key_manager.get_key_statuses())


async def forward_streaming(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    params: Dict[str, str],
    selected_key: str
) -> AsyncGenerator[bytes, None]:
    """Асинхронно форвардит streaming ответ"""
    try:
        # Создаем потоковый запрос
        async with client.stream(
            method=method,
            url=url,
            headers=headers,
            content=content,
            params=params,
            timeout=30.0
        ) as response:
            # Обработка ошибок
            if response.status_code == 429:
                key_manager.handle_rate_limit_response(selected_key, response)
                raise HTTPException(status_code=429, detail="Rate limited")

            if response.status_code != 200:
                error_body = await response.aread()
                logger.error(f"OpenRouter error: {response.status_code} - {error_body}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=error_body.decode("utf-8") if error_body else "OpenRouter API error"
                )

            # Логирование ответа
            logger.info(f"OpenRouter streaming response: {response.status_code}")
            logger.debug(f"Response headers: {sanitize_headers(dict(response.headers))}")

            # Стримим данные клиенту
            async for chunk in response.aiter_bytes():
                # Для отладки: логируем первый chunk
                yield chunk

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error: {str(e)}")
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Request failed: {str(e)}")
        raise HTTPException(status_code=500, detail="OpenRouter API unavailable")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_request(
    request: Request,
    path: str,
    authorization: str = Header(None, alias="Authorization"),
    apikey: str = Header(None, alias="APIKEY"),
):
    # Поддержка двух вариантов авторизации
    valid_auth = False
    if apikey and apikey == PROXY_API_KEY:
        valid_auth = True
    elif (
        authorization
        and authorization.startswith("Bearer ")
        and authorization.split(" ")[1] == PROXY_API_KEY
    ):
        valid_auth = True

    if not valid_auth:
        logger.warning("Invalid authentication")
        raise HTTPException(status_code=403, detail="Invalid authentication")

    # Определяем является ли запрос streaming
    is_streaming = False
    body_bytes = await request.body()

    try:
        if body_bytes:
            request_body = json.loads(body_bytes)
            is_streaming = request_body.get('stream', False)
    except json.JSONDecodeError:
        logger.warning("Failed to parse request body as JSON")

    # Получение доступного ключа
    selected_key = key_manager.get_available_key()
    if not selected_key:
        logger.error("All API keys are rate limited")
        raise HTTPException(
            status_code=429,
            detail="All API keys are rate limited. Try after 03:00 UTC.",
        )

    # Подготовка запроса к OpenRouter
    openrouter_url = f"https://openrouter.ai/api/v1/{path}"
    params = dict(request.query_params)

    # Логирование для отладки
    logger.info(f"Forwarding request to OpenRouter: {request.method} {openrouter_url}")
    logger.debug(f"Streaming: {is_streaming}")
    logger.debug(f"Request body: {body_bytes.decode('utf-8')}")

    # Для streaming запросов используем специальную обработку
    if is_streaming:
        # Создаем заголовки для OpenRouter
        openrouter_headers = {
            "Authorization": f"Bearer {selected_key}",
            "X-Title": "OpenrouterProxy",
            "Content-Type": "application/json",
            "Accept": "text/event-stream"
        }

        async def streaming_generator():
            async with httpx.AsyncClient() as client:
                try:
                    async for chunk in forward_streaming(
                        client=client,
                        method=request.method,
                        url=openrouter_url,
                        headers=openrouter_headers,
                        content=body_bytes,
                        params=params,
                        selected_key=selected_key
                    ):
                        yield chunk
                except HTTPException as e:
                    # Преобразуем HTTPException в формат, понятный клиенту
                    error_data = json.dumps({
                        "error": {
                            "message": e.detail,
                            "type": "api_error",
                            "code": e.status_code
                        }
                    }).encode('utf-8')
                    yield error_data
                except Exception as e:
                    logger.error(f"Unexpected error: {str(e)}")
                    error_data = json.dumps({
                        "error": {
                            "message": "Internal server error",
                            "type": "server_error",
                            "code": 500
                        }
                    }).encode('utf-8')
                    yield error_data

        return StreamingResponse(
            streaming_generator(),
            media_type="text/event-stream",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive"
            }
        )

    # Обработка не-streaming запросов
    async with httpx.AsyncClient() as client:
        try:
            # Подготовка заголовков для обычного запроса
            headers = {
                "Authorization": f"Bearer {selected_key}",
                "X-Title": "OpenrouterProxy",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            response = await client.request(
                method=request.method,
                url=openrouter_url,
                headers=headers,
                content=body_bytes,
                params=params,
                timeout=30.0
            )

            # Обработка лимита запросов
            if response.status_code == 429:
                key_manager.handle_rate_limit_response(selected_key, response)

                # Повтор с новым ключом
                new_key = key_manager.get_available_key()
                if new_key:
                    logger.info(f"Retrying with new key: {new_key[:5]}...")
                    headers["Authorization"] = f"Bearer {new_key}"
                    response = await client.request(
                        method=request.method,
                        url=openrouter_url,
                        headers=headers,
                        content=body_bytes,
                        params=params,
                        timeout=30.0
                    )
                else:
                    return JSONResponse(
                        status_code=429,
                        content={"error": "All keys exhausted. Try after 03:00 UTC."},
                    )

            # Возвращаем обычный ответ
            content = response.content
            logger.debug(f"Response body: {content[:500]}...")
            return Response(
                content=content,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.headers.get("content-type", "application/json")
            )

        except httpx.RequestError as e:
            logger.error(f"Request failed: {str(e)}")
            raise HTTPException(status_code=500, detail="OpenRouter API unavailable")


class KeyStatusResponse(BaseModel):
    keys: Dict[str, str]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
