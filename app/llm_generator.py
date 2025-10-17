"""
Upgraded llm_generator.py (Final)
---------------------------------
- Uses OpenAI Python client (OpenAI()) for GPT-4o if available.
- Falls back to app.llm_client.chat_completion() when OpenAI SDK not installed.
- Produces validated JSON response (keys: app_code, readme, license, metadata).
- Self-retries on malformed outputs.
- Includes schema validation, error handling, and robust JSON extraction.
"""

import json
import time
import logging
from typing import Dict, Any, Optional

# Import the fallback client
try:
    from . import llm_client  # ✅ fallback if OpenAI SDK not installed
except Exception as e:
    llm_client = None
    logging.warning("llm_client import failed: %s", e)

# Try native OpenAI SDK first
try:
    from openai import OpenAI
    _client = OpenAI()
except Exception:
    _client = None
    logging.warning("OpenAI SDK not found; will use llm_client fallback if available.")

# Optional schema validation support
try:
    import jsonschema
except Exception:
    jsonschema = None  # fallback to manual validation


# -----------------------------------------------------------
# ✅ JSON Schema Definition
# -----------------------------------------------------------
EXPECTED_SCHEMA = {
    "type": "object",
    "required": ["app_code", "readme", "license"],
    "properties": {
        # app_code may be a single string (monolithic archive) or an object mapping filenames to file contents
        "app_code": {"anyOf": [{"type": "string"}, {"type": "object"}]},
        "readme": {"type": "string"},
        "license": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "additionalProperties": True,
}

DEFAULT_MODEL = "gpt-4o"
DEFAULT_TEMPERATURE = 0.1


# -----------------------------------------------------------
# 🧩 Validation Helpers
# -----------------------------------------------------------
def _validate_json_schema(obj: Dict[str, Any]) -> bool:
    """Validate JSON against EXPECTED_SCHEMA."""
    if not isinstance(obj, dict):
        return False
    # Validate required keys with flexible rules for app_code
    if 'app_code' not in obj:
        return False
    app_code_val = obj['app_code']
    if isinstance(app_code_val, str):
        if not app_code_val.strip():
            return False
    elif isinstance(app_code_val, dict):
        # Ensure mapping keys and values are strings and non-empty
        if not app_code_val:
            return False
        for fk, fv in app_code_val.items():
            if not isinstance(fk, str) or not fk.strip():
                return False
            if not isinstance(fv, str) or not fv.strip():
                return False
    else:
        return False

    # readme and license must be present and strings
    for k in ("readme", "license"):
        if k not in obj or not isinstance(obj[k], str) or not obj[k].strip():
            return False
    if jsonschema is not None:
        try:
            jsonschema.validate(instance=obj, schema=EXPECTED_SCHEMA)
        except Exception as e:
            logging.debug("jsonschema validation failed: %s", e)
            return False
    return True


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Find and parse the first JSON object inside text (handles code fences)."""
    if not text or not isinstance(text, str):
        return None
    # Heuristic 1: remove common markdown fences to make the JSON visible
    cleaned = text.replace('\r\n', '\n')
    cleaned = cleaned.replace('```json', '```')
    cleaned = cleaned.strip()

    # If the whole cleaned text is JSON-like, try direct parse first
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Heuristic 2: find balanced-brace JSON snippets. Try every '{' as a start
    # and find the matching '}' by tracking brace depth. Return first valid parse.
    for i, ch in enumerate(cleaned):
        if ch != '{':
            continue
        depth = 0
        for j in range(i, len(cleaned)):
            if cleaned[j] == '{':
                depth += 1
            elif cleaned[j] == '}':
                depth -= 1
                if depth == 0:
                    candidate = cleaned[i:j + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        # not valid JSON, keep searching
                        break
        # continue to next '{'

    # Heuristic 3: try to strip markdown fences and surrounding text around code blocks
    # e.g. ```json\n{...}\n``` or ```\n{...}\n```
    import re
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except Exception:
            pass

    return None


# -----------------------------------------------------------
# 🧠 Core LLM Call
# -----------------------------------------------------------
def call_llm(prompt: str,
             model: str = DEFAULT_MODEL,
             temperature: float = DEFAULT_TEMPERATURE,
             max_tokens: int = 2400) -> str:
    """
    Call GPT-4o via the OpenAI SDK if available.
    Falls back to llm_client.chat_completion() if OpenAI SDK not available.
    """
    if _client is None:
        # ✅ Fallback path: use llm_client if available
        if llm_client is None:
            raise RuntimeError("Neither OpenAI SDK nor llm_client available.")
        logging.info("Using llm_client.chat_completion() fallback...")
        return llm_client.chat_completion(
            system="You are an expert code generator that outputs strict JSON with keys: app_code, readme, license, metadata.",
            user=prompt,
            model=model,
            temperature=temperature,
        )

    # ✅ Primary path: use OpenAI SDK
    try:
        response = _client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": "You are an expert code generator that outputs strict JSON with keys: app_code, readme, license, metadata."},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Extract text safely
        raw = None
        if hasattr(response, "choices") and response.choices:
            c = response.choices[0]
            if hasattr(c, "message") and hasattr(c.message, "content"):
                raw = c.message.content
            elif isinstance(c, dict) and "message" in c and "content" in c["message"]:
                raw = c["message"]["content"]
            elif hasattr(c, "text"):
                raw = c.text
        if not raw:
            raw = str(response)
        return raw

    except Exception as e:
        logging.exception("LLM call via OpenAI SDK failed: %s", e)
        raise


# -----------------------------------------------------------
# ⚙️ Generator Function
# -----------------------------------------------------------
def generate_app_from_brief(brief: str,
                            max_attempts: int = 3,
                            model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """
    Generate an app (app_code, readme, license) from a text brief.
    Includes retry, schema validation, and backoff.
    """
    prompt = (
        f"Generate a minimal, runnable web app (HTML/CSS/JS or Python static site). "
        f"Output strict JSON only with keys: app_code, readme, license, metadata. "
        f"Brief:\n{brief}\nEnsure JSON parsable output – no commentary."
    )

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            raw = call_llm(prompt, model)
            parsed = _extract_json_from_text(raw)
            if parsed and _validate_json_schema(parsed):
                logging.info("✅ LLM generated valid JSON on attempt %s", attempt)
                return parsed
            # Log the truncated raw output to aid debugging of parsing failures
            raw_preview = (raw[:1000] + '...') if raw and len(raw) > 1000 else raw
            logging.warning("Attempt %s: invalid JSON, retrying... Raw output preview: %s", attempt, raw_preview)
            last_err = RuntimeError("Invalid or unparsable LLM output.")
        except Exception as e:
            last_err = e
            logging.warning("Attempt %s failed: %s", attempt, e)
        time.sleep(2 ** (attempt - 1))
    logging.error("❌ LLM generation failed after %s attempts: %s", max_attempts, last_err)
    raise last_err or RuntimeError("LLM generation failed")


# -----------------------------------------------------------
# 🧪 Local Test
# -----------------------------------------------------------
if __name__ == "__main__":
    sample_brief = "Create a small static site that shows 'Hello World' and a README."
    try:
        result = generate_app_from_brief(sample_brief, max_attempts=1)
        print("Generated keys:", list(result.keys()))
    except Exception as e:
        print("LLM generator test failed:", e)
