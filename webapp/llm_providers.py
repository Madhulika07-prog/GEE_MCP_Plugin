"""Pluggable LLM provider layer for the GEE chat-map.

The app calls `run_chat(provider, model_id, history, user_message, tools, system, execute_tool)`
and the matching adapter handles the function-calling loop with that provider.

Internal history format (provider-agnostic):
    history: list[Turn] where Turn = {"role": "user" | "assistant", "blocks": list[Block]}
    Block:
        {"type": "text", "text": str}
        {"type": "tool_use", "id": str, "name": str, "args": dict}
        {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}

Each adapter:
1. Converts the internal history to its native format.
2. Runs its provider's tool-use loop.
3. Appends new turns back to `history` in the internal format.
4. Returns (assistant_text, frontend_ops).

Supported providers:
- google:    Gemini via google-genai (free Google AI Studio key)
- anthropic: Claude via anthropic SDK (paid)
- openai:    GPT via openai SDK (paid)
- groq:      Llama via openai SDK + Groq base URL (free tier)
- mistral:   Mistral via openai SDK + Mistral base URL (free tier on small models)
- openrouter: any model via openai SDK + OpenRouter base URL (mixed free/paid)
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import anthropic
from google import genai
from google.genai import types as genai_types
from openai import OpenAI

# ----------------------------------------------------------------------------
# Model catalog
# ----------------------------------------------------------------------------

@dataclass
class ModelSpec:
    provider: str
    model_id: str
    display: str
    free: bool
    notes: str = ""


# Order matters — frontend renders this order. Free models first within each group.
MODEL_CATALOG: list[ModelSpec] = [
    # --- Google Gemini (free tier on AI Studio key) ---
    ModelSpec("google", "gemini-2.5-flash", "Gemini 2.5 Flash", True, "Free tier · best default"),
    ModelSpec("google", "gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", True, "Free · fastest"),
    ModelSpec("google", "gemini-2.5-pro", "Gemini 2.5 Pro", False, "Paid · highest quality"),

    # --- Groq (free tier, very fast OSS models) ---
    ModelSpec("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B (Groq)", True, "Free · strong tool use"),
    ModelSpec("groq", "llama-3.1-8b-instant", "Llama 3.1 8B (Groq)", True, "Free · sub-second responses"),

    # --- Mistral (free tier on small) ---
    ModelSpec("mistral", "mistral-small-latest", "Mistral Small", True, "Free tier"),
    ModelSpec("mistral", "mistral-large-latest", "Mistral Large", False, "Paid"),

    # --- OpenRouter (free + paid models proxied) ---
    ModelSpec("openrouter", "meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B (OpenRouter free)", True),
    ModelSpec("openrouter", "google/gemini-2.0-flash-exp:free", "Gemini 2.0 Flash exp (OpenRouter free)", True),

    # --- Anthropic Claude (paid) ---
    ModelSpec("anthropic", "claude-opus-4-8", "Claude Opus 4.8", False, "Paid · best quality"),
    ModelSpec("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6", False, "Paid · balanced"),
    ModelSpec("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5", False, "Paid · cheapest"),

    # --- OpenAI (paid) ---
    ModelSpec("openai", "gpt-4o", "GPT-4o", False, "Paid"),
    ModelSpec("openai", "gpt-4o-mini", "GPT-4o mini", False, "Paid · cheaper"),
]

# Per-provider config (env var name + base URL for OpenAI-compatible providers).
PROVIDER_CONFIG = {
    "google":     {"env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "base_url": None},
    "anthropic":  {"env": ("ANTHROPIC_API_KEY",),               "base_url": None},
    "openai":     {"env": ("OPENAI_API_KEY",),                  "base_url": None},
    "groq":       {"env": ("GROQ_API_KEY",),                    "base_url": "https://api.groq.com/openai/v1"},
    "mistral":    {"env": ("MISTRAL_API_KEY",),                 "base_url": "https://api.mistral.ai/v1"},
    "openrouter": {"env": ("OPENROUTER_API_KEY",),              "base_url": "https://openrouter.ai/api/v1"},
}


def get_provider_key(provider: str) -> str | None:
    for env in PROVIDER_CONFIG[provider]["env"]:
        v = os.environ.get(env)
        if v:
            return v
    return None


def list_available_models() -> list[dict]:
    """Return a list of models annotated with whether their key is currently set."""
    out = []
    for spec in MODEL_CATALOG:
        key = get_provider_key(spec.provider)
        out.append({
            "provider": spec.provider,
            "model_id": spec.model_id,
            "display": spec.display,
            "free": spec.free,
            "notes": spec.notes,
            "available": key is not None,
            "key_env": PROVIDER_CONFIG[spec.provider]["env"][0],
        })
    return out


def get_spec(model_id: str) -> ModelSpec | None:
    return next((m for m in MODEL_CATALOG if m.model_id == model_id), None)


# ----------------------------------------------------------------------------
# Internal block helpers
# ----------------------------------------------------------------------------

def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use_block(name: str, args: dict, id_: str | None = None) -> dict:
    return {"type": "tool_use", "id": id_ or f"call_{uuid.uuid4().hex[:12]}", "name": name, "args": args}


def _tool_result_block(tool_use_id: str, content: Any, is_error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content if isinstance(content, str) else json.dumps(content),
        "is_error": is_error,
    }


# ----------------------------------------------------------------------------
# Adapter: Google Gemini
# ----------------------------------------------------------------------------

def _gemini_tools(tools: list[dict]) -> list[genai_types.Tool]:
    decls = []
    for t in tools:
        # Convert lowercase JSON-schema types to uppercase OpenAPI types Gemini expects.
        params = _to_uppercase_schema(t.get("parameters", {"type": "object", "properties": {}}))
        decls.append({"name": t["name"], "description": t.get("description", ""), "parameters": params})
    return [genai_types.Tool(function_declarations=decls)]


def _to_uppercase_schema(schema: dict) -> dict:
    """Recursively convert {'type': 'object'} to {'type': 'OBJECT'} for Gemini."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.upper()
        elif isinstance(v, dict):
            out[k] = _to_uppercase_schema(v)
        elif isinstance(v, list):
            out[k] = [_to_uppercase_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def _gemini_contents_from_history(history: list[dict]) -> list[genai_types.Content]:
    """Convert internal history to a list of google.genai Content objects."""
    out = []
    for turn in history:
        role = turn["role"]
        # Gemini uses "user" for both user messages AND tool-result responses.
        # The assistant role is "model".
        if role == "assistant":
            parts = []
            for b in turn["blocks"]:
                if b["type"] == "text":
                    parts.append(genai_types.Part.from_text(text=b["text"]))
                elif b["type"] == "tool_use":
                    parts.append(genai_types.Part.from_function_call(name=b["name"], args=b["args"]))
            if parts:
                out.append(genai_types.Content(role="model", parts=parts))
        else:  # user
            parts = []
            for b in turn["blocks"]:
                if b["type"] == "text":
                    parts.append(genai_types.Part.from_text(text=b["text"]))
                elif b["type"] == "tool_result":
                    try:
                        content_obj = json.loads(b["content"])
                    except (json.JSONDecodeError, TypeError):
                        content_obj = {"result": b["content"]}
                    # Find the tool name from the previous assistant turn's matching tool_use.
                    tool_name = _find_tool_name_for_id(history, b["tool_use_id"]) or "unknown"
                    payload = {"error": content_obj} if b.get("is_error") else {"result": content_obj}
                    parts.append(genai_types.Part.from_function_response(name=tool_name, response=payload))
            if parts:
                out.append(genai_types.Content(role="user", parts=parts))
    return out


def _find_tool_name_for_id(history: list[dict], tool_use_id: str) -> str | None:
    for turn in history:
        for b in turn["blocks"]:
            if b["type"] == "tool_use" and b["id"] == tool_use_id:
                return b["name"]
    return None


def _gemini_run(
    model_id: str,
    history: list[dict],
    user_message: str,
    tools: list[dict],
    system: str,
    execute_tool: Callable[[str, dict], tuple[Any, dict | None]],
) -> tuple[str, list[dict]]:
    client = genai.Client(api_key=get_provider_key("google"))
    history.append({"role": "user", "blocks": [_text_block(user_message)]})
    assistant_texts: list[str] = []
    frontend_ops: list[dict] = []
    # Disable thinking — on Gemini 2.5 Flash, thinking tokens count against
    # output and a complex tool surface can starve the response. Tool selection
    # is straightforward enough that the model doesn't need explicit reasoning.
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        tools=_gemini_tools(tools),
        temperature=0.4,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )
    for _ in range(8):
        contents = _gemini_contents_from_history(history)
        response = client.models.generate_content(model=model_id, contents=contents, config=config)
        if not response.candidates:
            break
        assistant_blocks = []
        function_calls = []
        for part in (response.candidates[0].content.parts or []):
            if part.text:
                assistant_blocks.append(_text_block(part.text))
                assistant_texts.append(part.text)
            if part.function_call:
                fc = part.function_call
                block = _tool_use_block(name=fc.name, args=dict(fc.args or {}))
                assistant_blocks.append(block)
                function_calls.append(block)
        history.append({"role": "assistant", "blocks": assistant_blocks})
        if not function_calls:
            break
        result_blocks = []
        for call_block in function_calls:
            try:
                model_result, op = execute_tool(call_block["name"], call_block["args"])
                result_blocks.append(_tool_result_block(call_block["id"], model_result))
                if op is not None:
                    frontend_ops.append(op)
            except Exception as e:
                result_blocks.append(_tool_result_block(
                    call_block["id"], f"{type(e).__name__}: {e}", is_error=True
                ))
        history.append({"role": "user", "blocks": result_blocks})
    return "\n\n".join(t.strip() for t in assistant_texts if t.strip()).strip(), frontend_ops


# ----------------------------------------------------------------------------
# Adapter: Anthropic Claude
# ----------------------------------------------------------------------------

def _anthropic_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": _to_lowercase_schema(t.get("parameters", {"type": "object", "properties": {}})),
        }
        for t in tools
    ]


def _to_lowercase_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif isinstance(v, dict):
            out[k] = _to_lowercase_schema(v)
        elif isinstance(v, list):
            out[k] = [_to_lowercase_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def _anthropic_messages_from_history(history: list[dict]) -> list[dict]:
    out = []
    for turn in history:
        content = []
        for b in turn["blocks"]:
            if b["type"] == "text":
                content.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                content.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["args"]})
            elif b["type"] == "tool_result":
                content.append({
                    "type": "tool_result",
                    "tool_use_id": b["tool_use_id"],
                    "content": b["content"],
                    **({"is_error": True} if b.get("is_error") else {}),
                })
        if content:
            out.append({"role": turn["role"], "content": content})
    return out


def _anthropic_run(
    model_id: str,
    history: list[dict],
    user_message: str,
    tools: list[dict],
    system: str,
    execute_tool: Callable[[str, dict], tuple[Any, dict | None]],
) -> tuple[str, list[dict]]:
    client = anthropic.Anthropic(api_key=get_provider_key("anthropic"))
    history.append({"role": "user", "blocks": [_text_block(user_message)]})
    assistant_texts: list[str] = []
    frontend_ops: list[dict] = []
    for _ in range(8):
        msgs = _anthropic_messages_from_history(history)
        response = client.messages.create(
            model=model_id, max_tokens=8000, system=system,
            tools=_anthropic_tools(tools), messages=msgs,
        )
        assistant_blocks = []
        for block in response.content:
            if block.type == "text":
                assistant_blocks.append(_text_block(block.text))
                assistant_texts.append(block.text)
            elif block.type == "tool_use":
                assistant_blocks.append({
                    "type": "tool_use", "id": block.id, "name": block.name, "args": dict(block.input),
                })
        history.append({"role": "assistant", "blocks": assistant_blocks})
        if response.stop_reason != "tool_use":
            break
        result_blocks = []
        for b in assistant_blocks:
            if b["type"] != "tool_use":
                continue
            try:
                model_result, op = execute_tool(b["name"], b["args"])
                result_blocks.append(_tool_result_block(b["id"], model_result))
                if op is not None:
                    frontend_ops.append(op)
            except Exception as e:
                result_blocks.append(_tool_result_block(
                    b["id"], f"{type(e).__name__}: {e}", is_error=True
                ))
        history.append({"role": "user", "blocks": result_blocks})
    return "\n\n".join(t.strip() for t in assistant_texts if t.strip()).strip(), frontend_ops


# ----------------------------------------------------------------------------
# Adapter: OpenAI-compatible (OpenAI / Groq / Mistral / OpenRouter)
# ----------------------------------------------------------------------------

def _openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _to_lowercase_schema(t.get("parameters", {"type": "object", "properties": {}})),
            },
        }
        for t in tools
    ]


def _openai_messages_from_history(history: list[dict], system: str) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for turn in history:
        if turn["role"] == "user":
            # If this turn contains tool_result blocks, emit them as `role: "tool"`
            # messages. Otherwise concatenate text.
            tool_results = [b for b in turn["blocks"] if b["type"] == "tool_result"]
            texts = [b for b in turn["blocks"] if b["type"] == "text"]
            for b in tool_results:
                out.append({
                    "role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"],
                })
            if texts:
                out.append({"role": "user", "content": "\n".join(b["text"] for b in texts)})
        else:  # assistant
            text_parts = [b["text"] for b in turn["blocks"] if b["type"] == "text"]
            tool_calls = []
            for b in turn["blocks"]:
                if b["type"] == "tool_use":
                    tool_calls.append({
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b["args"])},
                    })
            msg: dict[str, Any] = {"role": "assistant"}
            msg["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
    return out


def _openai_run(
    provider: str,
    model_id: str,
    history: list[dict],
    user_message: str,
    tools: list[dict],
    system: str,
    execute_tool: Callable[[str, dict], tuple[Any, dict | None]],
) -> tuple[str, list[dict]]:
    cfg = PROVIDER_CONFIG[provider]
    client_kwargs = {"api_key": get_provider_key(provider)}
    if cfg["base_url"]:
        client_kwargs["base_url"] = cfg["base_url"]
    client = OpenAI(**client_kwargs)

    history.append({"role": "user", "blocks": [_text_block(user_message)]})
    assistant_texts: list[str] = []
    frontend_ops: list[dict] = []
    oai_tools = _openai_tools(tools)
    for _ in range(8):
        msgs = _openai_messages_from_history(history, system)
        response = client.chat.completions.create(
            model=model_id, messages=msgs, tools=oai_tools or None, temperature=0.4,
        )
        choice = response.choices[0].message
        assistant_blocks = []
        if choice.content:
            assistant_blocks.append(_text_block(choice.content))
            assistant_texts.append(choice.content)
        if choice.tool_calls:
            for tc in choice.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                assistant_blocks.append({
                    "type": "tool_use", "id": tc.id, "name": tc.function.name, "args": args,
                })
        history.append({"role": "assistant", "blocks": assistant_blocks})
        if not choice.tool_calls:
            break
        result_blocks = []
        for b in assistant_blocks:
            if b["type"] != "tool_use":
                continue
            try:
                model_result, op = execute_tool(b["name"], b["args"])
                result_blocks.append(_tool_result_block(b["id"], model_result))
                if op is not None:
                    frontend_ops.append(op)
            except Exception as e:
                result_blocks.append(_tool_result_block(
                    b["id"], f"{type(e).__name__}: {e}", is_error=True
                ))
        history.append({"role": "user", "blocks": result_blocks})
    return "\n\n".join(t.strip() for t in assistant_texts if t.strip()).strip(), frontend_ops


# ----------------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------------

def run_chat(
    model_id: str,
    history: list[dict],
    user_message: str,
    tools: list[dict],
    system: str,
    execute_tool: Callable[[str, dict], tuple[Any, dict | None]],
) -> tuple[str, list[dict]]:
    spec = get_spec(model_id)
    if spec is None:
        raise ValueError(f"Unknown model id: {model_id}")
    if get_provider_key(spec.provider) is None:
        env = PROVIDER_CONFIG[spec.provider]["env"][0]
        raise ValueError(f"{spec.display} unavailable: set {env} in gee-mcp\\.env and restart.")
    if spec.provider == "google":
        return _gemini_run(model_id, history, user_message, tools, system, execute_tool)
    if spec.provider == "anthropic":
        return _anthropic_run(model_id, history, user_message, tools, system, execute_tool)
    # Everyone else speaks the OpenAI wire format.
    return _openai_run(spec.provider, model_id, history, user_message, tools, system, execute_tool)
