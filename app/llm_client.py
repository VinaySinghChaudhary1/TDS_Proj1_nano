# app/llm_client.py
import json
import logging
import time
from typing import Any, Dict, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .settings import settings

logger = logging.getLogger(__name__)

# Build base url (should be something like https://aipipe.org/openai/v1)
BASE_URL = settings.OPENAI_BASE_URL.rstrip("/")


class LLMError(Exception):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_exception_type((requests.RequestException, LLMError)))
def _post_chat(payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    """
    Low-level POST to the chat completions endpoint with retries.
    """
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    logger.debug("LLM request to %s payload keys=%s", url, list(payload.keys()))
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code >= 400:
        logger.warning("LLM API returned status %s: %s", r.status_code, r.text[:400])
        raise LLMError(f"LLM API error {r.status_code}")
    return r.json()


def chat_completion(system: str, user: str, model: Optional[str] = None, temperature: float = 0.0,
                    max_tokens: int = 2400) -> str:
    """
    Simple wrapper that calls the chat completions endpoint and returns the assistant text.
    """
    model_name = model or settings.AIMODEL_NAME
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    resp = _post_chat(payload)
    # The response format may vary; try to extract text safely
    try:
        # OpenAI style: choices[0].message.content
        if "choices" in resp and isinstance(resp["choices"], list) and resp["choices"]:
            choice = resp["choices"][0]
            if "message" in choice and isinstance(choice["message"], dict):
                return choice["message"].get("content", "").strip()
            # some endpoints return 'text'
            return choice.get("text", "").strip()
        # Fallback: top-level 'output' or 'result'
        for key in ("output", "result"):
            if key in resp:
                if isinstance(resp[key], str):
                    return resp[key].strip()
                if isinstance(resp[key], list) and resp[key]:
                    return str(resp[key][0]).strip()
        raise LLMError("Could not find assistant text in response")
    except Exception as exc:
        logger.exception("Error extracting assistant text: %s", exc)
        raise LLMError("Failed to parse LLM response") from exc
