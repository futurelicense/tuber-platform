"""Thin OpenAI-compatible chat client for Academy AI Tools (Groq by default)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class AIConfigError(RuntimeError):
    pass


class AIRequestError(RuntimeError):
    pass


def ai_configured():
    return bool((os.environ.get("AI_KEY") or os.environ.get("HF_API_KEY") or "").strip())


def ai_model_label():
    model = (os.environ.get("AI_MODEL") or "llama-3.3-70b-versatile").strip()
    short = model.split("/")[-1]
    return f"Groq · {short}"


def chat_completion(messages, max_tokens=900, temperature=0.5, retries=3):
    """POST chat completion using AI_KEY / AI_ENDPOINT / AI_MODEL (Groq-compatible)."""
    ai_key = (os.environ.get("AI_KEY") or os.environ.get("HF_API_KEY") or "").strip()
    if not ai_key:
        raise AIConfigError("AI_KEY is not configured.")

    endpoint = (
        os.environ.get("AI_ENDPOINT")
        or "https://api.groq.com/openai/v1/chat/completions"
    ).strip()
    model = (os.environ.get("AI_MODEL") or "llama-3.3-70b-versatile").strip()

    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode("utf-8")

    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {ai_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "MoneyTuber-Academy/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            content = (result["choices"][0]["message"]["content"] or "").strip()
            if not content:
                raise AIRequestError("Empty response from AI provider.")
            return content
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:400]
            last_err = e
            if e.code == 429 and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After") or e.headers.get("retry-after")
                try:
                    wait = float(retry_after)
                except (TypeError, ValueError):
                    wait = 8.0 * (attempt + 1)
                time.sleep(min(wait, 30.0))
                continue
            raise AIRequestError(f"AI provider returned {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise AIRequestError(f"Could not reach AI provider: {e.reason}") from e

    raise AIRequestError(f"AI request failed after retries: {last_err}")


TOOL_MODES = {
    "chat": {
        "label": "Chat",
        "system": (
            "You are MoneyTuber Academy Assistant — a practical AI coach for YouTube "
            "creators and operators. Be concise, concrete, and actionable. Prefer short "
            "paragraphs and bullets. No fluff intros about being an AI."
        ),
    },
    "advice": {
        "label": "Give advice",
        "system": (
            "You give practical creator/business advice for YouTube operators. "
            "Ask clarifying questions only if essential. End with 3 concrete next steps."
        ),
    },
    "ideas": {
        "label": "Generate ideas",
        "system": (
            "You brainstorm YouTube / Shorts / content / offer ideas. "
            "Return a numbered list of 8–12 ideas with one-line why each works."
        ),
    },
    "summarize": {
        "label": "Summarize text",
        "system": (
            "Summarize the user's text for a busy creator. Use: 1) one-sentence overview, "
            "2) 5 key bullets, 3) one action takeaway."
        ),
    },
    "translate": {
        "label": "Translate",
        "system": (
            "Translate the user's text. If they don't specify a target language, "
            "translate to clear English and note the detected source language. "
            "Preserve meaning; keep marketing tone natural."
        ),
    },
}


def build_messages(mode, user_text, history=None):
    """Build provider messages from mode + optional prior turns (user/assistant only)."""
    spec = TOOL_MODES.get(mode) or TOOL_MODES["chat"]
    messages = [{"role": "system", "content": spec["system"]}]
    if history:
        for turn in history[-8:]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": user_text.strip()[:8000]})
    return messages
