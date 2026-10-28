"""LLM tool-calling loops (Claude primary, LM Studio fallback)."""
import json
import os

import anthropic
from dotenv import load_dotenv
from openai import OpenAI

from agent_harness.tools import SYSTEM_PROMPT, TOOLS, TOOLS_OPENAI, execute_tool

MAX_ITERATIONS = 10


def _build_client() -> anthropic.Anthropic:
    load_dotenv()
    return anthropic.Anthropic()


def _build_lmstudio_client() -> OpenAI:
    load_dotenv()
    return OpenAI(
        base_url=os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        api_key="lm-studio",  # unused by LM Studio, but the OpenAI client requires a value
    )


def _run_claude(user_prompt: str, client: anthropic.Anthropic, model: str) -> str:
    messages = [{"role": "user", "content": user_prompt}]

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        if not tool_use_blocks:
            return "".join(block.text for block in response.content if block.type == "text")

        tool_results = []
        for block in tool_use_blocks:
            content, is_error = execute_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return f"Unable to reach a final answer after {MAX_ITERATIONS} tool-calling iterations."


def _run_lmstudio(user_prompt: str, client: OpenAI, model: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for _ in range(MAX_ITERATIONS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS_OPENAI
        )
        message = response.choices[0].message

        if not message.tool_calls:
            return message.content

        messages.append(
            {"role": "assistant", "content": message.content, "tool_calls": message.tool_calls}
        )
        for tool_call in message.tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as exc:
                content = json.dumps({"error": f"Invalid tool arguments JSON: {exc}"})
            else:
                content, _is_error = execute_tool(tool_call.function.name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": content,
                }
            )

    return f"Unable to reach a final answer after {MAX_ITERATIONS} tool-calling iterations."


def _lmstudio_model(fallback_model: str | None, reason: str) -> str:
    if fallback_model:
        return fallback_model
    load_dotenv()
    model = os.environ.get("LMSTUDIO_MODEL")
    if not model:
        raise RuntimeError(f"{reason} and LMSTUDIO_MODEL is not set in .env — "
                           "cannot use LM Studio.") from None
    return model


def _backend() -> str:
    load_dotenv()
    backend = os.environ.get("LLM_BACKEND", "claude").strip().lower() or "claude"
    if backend not in ("claude", "lmstudio"):
        raise ValueError(f"LLM_BACKEND must be 'claude' or 'lmstudio', got {backend!r}")
    return backend


def run_agent(
    user_prompt: str,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    fallback_client: OpenAI | None = None,
    fallback_model: str | None = None,
) -> str:
    """LLM_BACKEND=claude (default): Claude, falling back to LM Studio when its
    API is unreachable. LLM_BACKEND=lmstudio: LM Studio only, Claude is never
    contacted. Any other value raises ValueError."""
    if _backend() == "lmstudio":
        lm_model = _lmstudio_model(fallback_model, "LLM_BACKEND=lmstudio")
        if fallback_client is None:
            fallback_client = _build_lmstudio_client()
        return _run_lmstudio(user_prompt, fallback_client, lm_model)

    if client is None:
        client = _build_client()
    if model is None:
        load_dotenv()
        model = os.environ.get("MODEL_NAME", "claude-opus-5")

    try:
        return _run_claude(user_prompt, client, model)
    except anthropic.APIConnectionError:
        # Claude's API is unreachable (network/DNS/timeout) — fall back to a
        # local LM Studio server. Auth and rate-limit errors are NOT caught
        # here, so a misconfigured API key still fails loudly.
        lm_model = _lmstudio_model(fallback_model, "Claude API is unreachable")
        if fallback_client is None:
            fallback_client = _build_lmstudio_client()
        return _run_lmstudio(user_prompt, fallback_client, lm_model)
