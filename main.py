import os
import logging
from typing import List, Dict, Optional, AsyncGenerator
import pendulum
import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
import json
import time
import backoff
import asyncio
import functools

app = FastAPI()

# Logging setup
log_level_name = os.getenv("UVICORN_LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level)
logger = logging.getLogger("openrouter-proxy")

# Configuration loading
PROXY_API_KEY = os.getenv("PROXY_API_KEY")
if not PROXY_API_KEY:
    logger.error("Error: Environment variable PROXY_API_KEY is not set.")
    exit(1)

OPENROUTER_KEYS = [
    k.strip() for k in os.getenv("OPENROUTER_KEYS", "").split(",") if k.strip()
]
if not OPENROUTER_KEYS:
    logger.error("Error: Environment variable OPENROUTER_KEYS is not set or empty.")
    exit(1)
TIMEZONE = os.getenv("TIMEZONE", "UTC")

# Initialize key status
key_status: Dict[str, Optional[pendulum.DateTime]] = {}

# Retry for non-streaming requests
@backoff.on_predicate(
    backoff.expo,
    predicate=lambda r: isinstance(r, httpx.Response) and r.status_code == 429 and check_retryable_error(r),
    max_tries=15,
    jitter=None,
    base=1.7
)

# Retry for streaming requests
def async_retryable(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        last_exception = None
        for attempt in range(15):
            try:
                async for value in func(*args, **kwargs):
                    yield value
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.info(f"Attempt {attempt + 1} failed with status 429, retrying...")
                    last_exception = e
                    await asyncio.sleep(1.7 ** attempt)
                else:
                    raise
            except Exception as e:
                logger.info(f"Attempt {attempt + 1} failed with exception: {e}, retrying...")
                last_exception = e
                await asyncio.sleep(1.7 ** attempt)
        else:
            # If the loop completes without breaking, all retries have been exhausted.
            raise last_exception
    return wrapper

async def make_openrouter_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    params: Dict[str, str],
    timeout: float
):
    try:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=content,
            params=params,
            timeout=timeout
        )
        return response
    except httpx.RequestError as e:
        logger.warning(f"Request error: {str(e)}")
        return None

def check_retryable_error(response: httpx.Response) -> bool:
    """
    Checks if the error in the httpx.Response is retryable based on its content.
    """
    try:
        error_content = response.json()
        error_message = error_content.get("error", {}).get("message", "")
        error_code = error_content.get("error", {}).get("code", None)

        if error_code == 429 and "Provider returned error" in error_message:
            return True

    except json.JSONDecodeError:
        logger.warning("Failed to decode JSON from response body during retry check")
    except Exception as e:
        logger.error(f"Unexpected error checking retryable error: {str(e)}")

    return False

@app.on_event("startup")
def initialize_key_status():
    global key_status
    if not OPENROUTER_KEYS:
        logger.error("No OPENROUTER_KEYS provided! Exiting...")
        exit(1)

    key_status = {key: None for key in OPENROUTER_KEYS}
    logger.info(f"Initialized with {len(OPENROUTER_KEYS)} API keys")


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Removes sensitive data from headers"""
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
    """Logs request details in debug mode"""
    # Clean headers
    sanitized_headers = sanitize_headers(headers)

    # Parse body if it's JSON
    body_info = None
    if body:
        try:
            body_info = json.loads(body.decode("utf-8"))
        except:
            body_info = f"Binary data ({len(body)} bytes)"

    # Form loggable object
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
    def __init__(self):
        self.current_index = 0
        self.success_counts: Dict[str, int] = {key: 0 for key in OPENROUTER_KEYS}

    def get_available_key(self) -> Optional[str]:
        if not OPENROUTER_KEYS:
            return None

        # Cyclic key selection
        current_time = pendulum.now(TIMEZONE)
        for i in range(len(OPENROUTER_KEYS)):
            key = OPENROUTER_KEYS[(self.current_index + i) % len(OPENROUTER_KEYS)]
            lock_time = key_status[key]
            if lock_time is None or current_time >= lock_time:
                if self.success_counts[key] < 10:
                    # Move to the next key
                    self.current_index = (self.current_index + i + 1) % len(OPENROUTER_KEYS)
                    return key
                else:
                    logger.warning(f"Key {key[:5]}... has reached the maximum success count and is temporarily blocked.")

        return None

    def block_key_until_next_day(self, key: str):
        utc_midnight = pendulum.today('UTC').add(days=1).to_datetime_string()
        unlock_time = pendulum.parse(utc_midnight).in_timezone(TIMEZONE)
        key_status[key] = unlock_time

        logger.warning(
            f"Key {key[:5]}... blocked until {unlock_time.to_iso8601_string()}"
        )

    async def handle_rate_limit_response(self, key: str, response: httpx.Response):
        try:
            try:
                error_content_bytes = await response.aread()
                # Now, attempt to decode the bytes to JSON
                error_content = json.loads(error_content_bytes.decode('utf-8'))
            except json.decoder.JSONDecodeError:
                error_content = None

            if error_content:
                error_message = error_content.get("error", {}).get("message", "")
                error_code = error_content.get("error", {}).get("code", None)

                if error_code == 429 and "free-models-per-day" in error_message:
                    self.block_key_until_next_day(key)
                else:
                    logger.warning(f"Key {key[:5]}... is temporarily rate-limited upstream.")
            else:
                logger.warning("Response body is not JSON, cannot determine rate limit type.")

        except Exception as e:
            logger.error(f"Unexpected error handling rate limit response: {str(e)}")

    def get_key_statuses(self) -> Dict[str, str]:
        current_time = pendulum.now(TIMEZONE)
        statuses = {}
        for key, lock_time in key_status.items():
            if lock_time is None:
                statuses[key] = "active" if self.success_counts[key] < 10 else "max_success_reached"
            elif current_time >= lock_time:
                statuses[key] = "active"  if self.success_counts[key] < 10 else "max_success_reached"
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

@async_retryable
async def forward_streaming(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    params: Dict[str, str],
    selected_key: str
) -> AsyncGenerator[bytes, None]:
    """Asynchronously forwards streaming response with backoff"""
    try:
        async with client.stream(
            method=method,
            url=url,
            headers=headers,
            content=content,
            params=params,
            timeout=30.0
        ) as response:
            if response.status_code == 429:
                await key_manager.handle_rate_limit_response(selected_key, response)
                raise HTTPException(status_code=429, detail="Rate limited")

            if response.status_code != 200:
                try:
                    error_body = await response.aread()
                    error_detail = error_body.decode("utf-8") if error_body else "OpenRouter API error"
                except Exception as e:
                    error_detail = f"Failed to read error body: {str(e)}"

                logger.error(f"OpenRouter error: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=error_detail
                )

            logger.info(f"OpenRouter streaming response: {response.status_code}")
            logger.debug(f"Response headers: {sanitize_headers(dict(response.headers))}")

            async for chunk in response.aiter_bytes():
                yield chunk

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error: {str(e)}")
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Request failed: {str(e)}")
        raise HTTPException(status_code=500, detail="OpenRouter API unavailable")

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_request(
    request: Request,
    path: str,
    authorization: str = Header(None, alias="Authorization"),
    apikey: str = Header(None, alias="APIKEY"),
):
    # Support two authentication methods
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

    # Determine if request is streaming
    is_streaming = False
    body_bytes = await request.body()

    try:
        if body_bytes:
            request_body = json.loads(body_bytes)
            is_streaming = request_body.get('stream', False)
    except json.JSONDecodeError:
        logger.warning("Failed to parse request body as JSON")

    # Get available key
    selected_key = key_manager.get_available_key()
    if not selected_key:
        logger.error("All API keys are rate limited")
        raise HTTPException(
            status_code=429,
            detail="All API keys are rate limited. Try after 03:00 UTC.",
        )

    # Prepare request to OpenRouter
    openrouter_url = f"https://openrouter.ai/api/v1/{path}"
    params = dict(request.query_params)

    # Logging for debugging
    logger.info(f"Forwarding request to OpenRouter: {request.method} {openrouter_url}")
    logger.debug(f"Streaming: {is_streaming}")
    logger.debug(f"Request body: {body_bytes.decode('utf-8')}")

    # For streaming requests, use special handling
    if is_streaming:
        # Create headers for OpenRouter
        openrouter_headers = {
            "Authorization": f"Bearer {selected_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Origin": "https://openrouter.ai",
            "Referer": "https://openrouter.ai",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
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
                    # Convert HTTPException to client-understandable format
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

    # Handle non-streaming requests
    async with httpx.AsyncClient() as client:
        try:
            headers = {
                "Authorization": f"Bearer {selected_key}",
                "X-Title": "OpenrouterProxy",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            response = await make_openrouter_request(
                client=client,
                method=request.method,
                url=openrouter_url,
                headers=headers,
                content=body_bytes,
                params=params,
                timeout=10.0
            )

            # Handle request limit
            if response.status_code == 429:
                await key_manager.handle_rate_limit_response(selected_key, response)
                new_key = key_manager.get_available_key()
                if new_key:
                    logger.info(f"Retrying with new key: {new_key[:5]}...")
                    headers["Authorization"] = f"Bearer {new_key}"
                    response = await make_openrouter_request(
                        client=client,
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

            content = response.content

            if response.status_code == 200:
                key_manager.increment_success_count(selected_key)

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

    uvicorn.run(app)
