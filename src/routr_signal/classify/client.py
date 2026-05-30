"""Provider-pluggable LLM client for JSON classification + drafting.

Selection is driven entirely by env vars:

    ROUTR_SIGNAL_LLM_PROVIDER   one of: anthropic | gemini | openai   (default: anthropic)
    ROUTR_SIGNAL_LLM_MODEL      provider-specific model ID            (default: see DEFAULT_MODELS)

We deliberately keep the API surface tiny: `call_json(system, user)` returns a parsed
JSON object. Each provider implementation is robust to:
  - JSON wrapped in ```json fences
  - JSON with prose preamble
  - balanced-brace recovery if the model wraps in extra text

If the configured provider's SDK or API errors out, we retry up to MAX_ATTEMPTS
with exponential backoff. Unrecoverable failures raise — callers catch upstream.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from typing import Any, Callable

from ..lib.env import env, env_required
from ..lib.logging import debug, info, warn


# Default model per provider for the CLASSIFIER tier (cheap, high-volume).
DEFAULT_CLASSIFIER_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-3-flash-preview",
    "openai": "gpt-5-mini",
}

# Default model per provider for the DRAFTER tier (flagship, low-volume, prose-heavy).
DEFAULT_DRAFTER_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-4-7",
    "gemini": "gemini-3.1-pro-preview",
    "openai": "gpt-5",
}

DEFAULT_MAX_TOKENS = 4096
MAX_ATTEMPTS = 3


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def provider_and_model(role: str = "classifier") -> tuple[str, str]:
    """Return (provider, model) honoring env overrides for the given role.

    `role` is `classifier` (cheap, high volume) or `drafter` (flagship, low volume).
    Env precedence for `role=drafter`:
        ROUTR_SIGNAL_DRAFTER_PROVIDER -> ROUTR_SIGNAL_LLM_PROVIDER -> "gemini"
        ROUTR_SIGNAL_DRAFTER_MODEL    -> DEFAULT_DRAFTER_MODELS[provider]
    Env precedence for `role=classifier`:
        ROUTR_SIGNAL_LLM_PROVIDER  -> "anthropic"
        ROUTR_SIGNAL_LLM_MODEL     -> DEFAULT_CLASSIFIER_MODELS[provider]
    """

    if role not in ("classifier", "drafter"):
        raise ValueError(f"unknown LLM role: {role!r}")

    if role == "drafter":
        provider = (
            env("ROUTR_SIGNAL_DRAFTER_PROVIDER")
            or env("ROUTR_SIGNAL_LLM_PROVIDER")
            or "gemini"
        ).lower().strip()
        if provider not in DEFAULT_DRAFTER_MODELS:
            raise ValueError(
                f"drafter provider={provider!r} invalid. Must be one of: "
                f"{sorted(DEFAULT_DRAFTER_MODELS)}"
            )
        model = env("ROUTR_SIGNAL_DRAFTER_MODEL") or DEFAULT_DRAFTER_MODELS[provider]
        return provider, model

    # role == "classifier"
    provider = (env("ROUTR_SIGNAL_LLM_PROVIDER") or "anthropic").lower().strip()
    if provider not in DEFAULT_CLASSIFIER_MODELS:
        raise ValueError(
            f"classifier provider={provider!r} invalid. Must be one of: "
            f"{sorted(DEFAULT_CLASSIFIER_MODELS)}"
        )
    model = env("ROUTR_SIGNAL_LLM_MODEL") or DEFAULT_CLASSIFIER_MODELS[provider]
    return provider, model


def call_json(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    role: str = "classifier",
) -> dict[str, Any]:
    """Send a prompt to the configured LLM and parse the JSON response.

    `role` picks between the cheap classifier tier and the flagship drafter tier.
    Explicit `model=` overrides the role default.
    """

    provider, default_model = provider_and_model(role=role)
    chosen_model = model or default_model
    info(f"llm[{role}]: provider={provider} model={chosen_model}")

    impl = _IMPLS[provider]

    last_exc: Exception | None = None
    last_text: str = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            text = impl(system=system, user=user, model=chosen_model, max_tokens=max_tokens)
        except _RetryableError as e:
            last_exc = e
            warn(f"llm[{role}/{provider}]: retryable error attempt {attempt + 1}: {e}")
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{provider} call failed after {MAX_ATTEMPTS} attempts: {e}") from e
        except Exception as e:
            raise RuntimeError(f"{provider} call failed: {e}") from e

        last_text = text
        debug(f"llm[{role}/{provider}]: raw response: {text[:500]}")
        try:
            return _extract_json(text)
        except json.JSONDecodeError as e:
            last_exc = e
            warn(
                f"llm[{role}/{provider}]: JSON parse failed attempt {attempt + 1}: {e}; "
                f"first 200 chars: {text[:200]}"
            )
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(1)
                continue

    raise RuntimeError(
        f"{provider} returned unparseable JSON after {MAX_ATTEMPTS} attempts. "
        f"Last text (truncated): {last_text[:400]}"
    ) from last_exc


# -----------------------------------------------------------------------------
# Provider implementations
# -----------------------------------------------------------------------------

class _RetryableError(Exception):
    """Marker for transient errors that should trigger a retry."""


# ---- Anthropic ----
_anthropic_client: Any = None


def _anthropic_call(*, system: str, user: str, model: str, max_tokens: int) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.Anthropic(api_key=env_required("ANTHROPIC_API_KEY"))

    import anthropic

    try:
        resp = _anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            temperature=0.0,  # Maximize determinism; still has tie-break variance
            messages=[{"role": "user", "content": user}],
        )
    except (anthropic.APIStatusError, anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        raise _RetryableError(str(e)) from e
    except anthropic.APIError as e:
        # Could be 4xx (non-retryable). Inspect status.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is None or 500 <= status < 600 or status == 429:
            raise _RetryableError(str(e)) from e
        raise

    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )


# ---- Gemini ----
_gemini_client: Any = None


def _gemini_call(*, system: str, user: str, model: str, max_tokens: int) -> str:
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(api_key=env_required("GEMINI_API_KEY"))

    from google.genai import errors as genai_errors
    from google.genai import types

    try:
        resp = _gemini_client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                max_output_tokens=max_tokens,
                temperature=0.0,  # Maximize determinism
                # Gemini 3 Flash supports MINIMAL for fastest JSON output on classification.
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            ),
        )
    except genai_errors.APIError as e:
        status = getattr(e, "code", None) or getattr(e, "status_code", None)
        if status is None or status in (429, 500, 502, 503, 504):
            raise _RetryableError(str(e)) from e
        raise

    text = (resp.text or "").strip()
    if not text:
        # Some thinking responses surface text via candidates[0].content.parts.
        try:
            text = "".join(p.text or "" for p in resp.candidates[0].content.parts if hasattr(p, "text"))
        except Exception:
            text = ""
    return text


# ---- OpenAI ----
_openai_client: Any = None


def _openai_call(*, system: str, user: str, model: str, max_tokens: int) -> str:
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI(api_key=env_required("OPENAI_API_KEY"))

    import openai as openai_sdk

    try:
        # GPT-5 reasoning models reject `temperature` parameter; only set when supported.
        kwargs: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if not model.startswith(("o1", "o3", "o4", "gpt-5")):
            kwargs["temperature"] = 0.0
        resp = _openai_client.chat.completions.create(**kwargs)
    except (openai_sdk.APITimeoutError, openai_sdk.APIConnectionError, openai_sdk.RateLimitError) as e:
        raise _RetryableError(str(e)) from e
    except openai_sdk.APIStatusError as e:
        if 500 <= e.status_code < 600:
            raise _RetryableError(str(e)) from e
        raise

    choice = resp.choices[0] if resp.choices else None
    if choice and choice.message and choice.message.content:
        return choice.message.content
    return ""


_IMPLS: dict[str, Callable[..., str]] = {
    "anthropic": _anthropic_call,
    "gemini": _gemini_call,
    "openai": _openai_call,
}


# -----------------------------------------------------------------------------
# JSON extraction (shared)
# -----------------------------------------------------------------------------

def _extract_json(text: str) -> dict[str, Any]:
    """Find the first balanced JSON object in `text` and parse it.

    Robust to ```json fences (balanced OR truncated), prose preambles, and
    trailing chatter. Last resort: balanced-brace walker with quote-state
    tracking so a `}` inside a string doesn't fool us.
    """

    text = text.strip()

    # Strip leading code fence, balanced or not.
    if text.startswith("```"):
        # Drop opening "```" plus optional "json" tag plus whitespace.
        text = re.sub(r"^```\s*(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
    # Strip trailing fence if present.
    if text.rstrip().endswith("```"):
        text = text.rstrip()
        text = text[:-3].rstrip()
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try a balanced fence match next (handles preamble + ```json ... ``` form).
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(fence_match.group(1).strip())

    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found in response.", text, 0)

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise json.JSONDecodeError("Unbalanced JSON object.", text, start)
