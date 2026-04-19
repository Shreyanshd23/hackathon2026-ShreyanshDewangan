"""
Groq LLM Client
────────────────
Thin wrapper around the Groq SDK for chat completions with tool calling.
"""

import json, time
from groq import Groq
from google import genai
from config import GROQ_API_KEY, GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE, LLM_MAX_TOKENS

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def chat(messages: list, tools: list | None = None, retries: int = 3) -> object:
    """
    Send a chat completion request to Groq.
    Returns the first choice's message object.
    Retries on transient errors with exponential backoff.
    """
    for attempt in range(retries):
        try:
            kwargs = dict(
                model=LLM_MODEL,
                messages=messages,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
            )
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            resp = groq_client.chat.completions.create(**kwargs)
            return resp.choices[0].message
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[LLM] attempt {attempt+1} failed ({exc}), retrying in {wait}s …")
                time.sleep(wait)
            else:
                raise


def chat_json(messages: list, retries: int = 3) -> dict:
    """
    Chat expecting a JSON response.  Tries to parse the content.
    Falls back to an empty dict on parse failure.
    """
    msg = chat(messages, tools=None, retries=retries)
    text = (msg.content or "").strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {"raw": text, "parse_error": True}


def gemini_evaluate(prompt: str) -> dict:
    """
    Use Gemini 2.0 Flash as an independent judge.
    Returns structured JSON evaluation.
    """
    if not gemini_client:
        return {"error": "Gemini API key missing"}

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
            }
        )
        return json.loads(response.text)
    except Exception as e:
        return {"error": f"Gemini evaluation failed: {str(e)}"}
