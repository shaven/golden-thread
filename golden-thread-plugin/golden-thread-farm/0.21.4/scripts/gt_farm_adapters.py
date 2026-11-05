#!/usr/bin/env python3
"""gt_farm_adapters.py -- the farm's ONE request/reply adapter table (gt-farm, gt 0.22.0). Standard library only.

A service-table row's `api` column (default `openai-chat`) names how a model speaks. Each adapter says:

    build(messages, params) -> args     the request arguments for a chat message list; `params` may carry
                                        temperature, max_tokens, model and json_schema
    select                              the LOTR `select` string that keeps the reply, its token counts and
                                        its stop reason (finish_of) only
    input_names                         the input names that identify a chat operation in discovery
    parse(data) -> (text, in_tokens, out_tokens, model)
                                        raises ReplyShape when the reply is not in this API's shape; a token
                                        count the reply does not carry is None (the caller estimates it)
    supports                            {json_schema, tools, stream}: what the API can do, said once
    schema_input                        the operation input that carries a bound schema (where supports.json_schema)
    direct, path(engine), headers(key)  whether the farm can call this API on a base_url itself, the URL path under
                                        the base_url, and the auth/version headers (the key is only ever put in a
                                        header, never in a URL, a log or a ledger line)

A new provider is one adapter in ADAPTERS and nothing else. An `api` value that is not in the table is refused
by name (UnknownApi), never guessed from a model name or a URL. `choices[0].message.content` is read here and
nowhere else in the farm: the direct wire, the relay and the LOTR path all parse through `parse`.

gt core keeps its own reply seam (gt_model_calibrate.REPLY_ADAPTERS) because core may not import an optional
module; tests/test_farm_adapters.py asserts the two agree on every shape they both name.
"""
import json

DEFAULT_API = "openai-chat"
DEFAULT_MAX_TOKENS = 1024        # for an API that insists on max_tokens when the caller named none


class UnknownApi(ValueError):
    """The service row names an `api` this table does not have."""


class ReplyShape(ValueError):
    """The reply is not in the shape its adapter reads."""


def _split(messages):
    """-> (system text, the rest): system messages joined, every other message in order."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    return system, [m for m in messages if m.get("role") != "system"]


def _count(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _blocks_text(blocks):
    """The text of a list of content blocks ({type?, text}); blocks of another type are skipped."""
    return "".join(str(b.get("text", "")) for b in blocks
                   if isinstance(b, dict) and b.get("type", "text") == "text")


# ---- openai-chat ------------------------------------------------------------------------------

def _build_openai(messages, params):
    args = {"messages": list(messages)}
    for k in ("temperature", "max_tokens", "model"):
        if params.get(k) is not None:
            args[k] = params[k]
    if params.get("max_tokens") is not None:        # newer OpenAI-compatible models reject `max_tokens` and want this
        args["max_completion_tokens"] = params["max_tokens"]       # one; older ones ignore it, so both go, equal
    if params.get("json_schema"):
        args["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "return_contract", "schema": params["json_schema"]}}
    if params.get("tools"):                         # OpenAI function tools; role 'tool' and 'tool_calls' messages go through as given
        args["tools"] = params["tools"]
        if params.get("tool_choice") is not None:
            args["tool_choice"] = params["tool_choice"]
    return args


def _full_openai(data):
    """-> the whole reply of one chat turn: text, tool_calls [{id, name, arguments (raw JSON string)}], reasoning,
    finish_reason, in_tokens, out_tokens, reasoning_tokens, cost_usd (None when the reply carries none), model."""
    try:
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage") or {}
        model = data.get("model")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ReplyShape("no choices[0].message in the reply")
    calls = []
    for c in (msg.get("tool_calls") or []):
        fn = c.get("function") or {} if isinstance(c, dict) else {}
        args = fn.get("arguments")
        calls.append({"id": str(c.get("id") or ""), "name": str(fn.get("name") or ""),
                      "arguments": args if isinstance(args, str) else json.dumps(args if args is not None else {})})
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    details = usage.get("completion_tokens_details") or {}
    cost = usage.get("cost")
    return {"text": msg.get("content") or "", "tool_calls": calls,
            "reasoning": reasoning if isinstance(reasoning, str) else "",
            "finish_reason": choice.get("finish_reason"),
            "in_tokens": _count(usage.get("prompt_tokens")), "out_tokens": _count(usage.get("completion_tokens")),
            "reasoning_tokens": _count(details.get("reasoning_tokens")) or 0,
            "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "model": model}


def _parse_openai(data):
    try:
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        model = data.get("model")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ReplyShape("no choices[0].message.content in the reply")
    return text or "", _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens")), model


# ---- anthropic-messages -----------------------------------------------------------------------

def _build_anthropic(messages, params):
    system, rest = _split(messages)
    args = {"messages": rest, "max_tokens": params.get("max_tokens") or DEFAULT_MAX_TOKENS}
    if system:
        args["system"] = system
    for k in ("temperature", "model"):
        if params.get(k) is not None:
            args[k] = params[k]
    return args


def _parse_anthropic(data):
    try:
        text = _blocks_text(data["content"])
        usage = data.get("usage") or {}
        model = data.get("model")
    except (KeyError, TypeError, AttributeError):
        raise ReplyShape("no content[].text in the reply")
    return text, _count(usage.get("input_tokens")), _count(usage.get("output_tokens")), model


# ---- gemini-generate --------------------------------------------------------------------------

def _build_gemini(messages, params):
    system, rest = _split(messages)
    args = {"contents": [{"role": "model" if m.get("role") == "assistant" else "user",
                          "parts": [{"text": str(m.get("content") or "")}]} for m in rest]}
    if system:
        args["systemInstruction"] = {"parts": [{"text": system}]}
    cfg = {}
    if params.get("temperature") is not None:
        cfg["temperature"] = params["temperature"]
    if params.get("max_tokens"):
        cfg["maxOutputTokens"] = params["max_tokens"]
    if params.get("json_schema"):
        cfg.update(responseMimeType="application/json", responseSchema=params["json_schema"])
    if cfg:
        args["generationConfig"] = cfg
    if params.get("model") is not None:             # a path placeholder in the connection's operation
        args["model"] = params["model"]
    return args


def _parse_gemini(data):
    try:
        text = "".join(str(p.get("text", "")) for p in data["candidates"][0]["content"]["parts"])
        usage = data.get("usageMetadata") or {}
        model = data.get("modelVersion")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ReplyShape("no candidates[0].content.parts[].text in the reply")
    return text, _count(usage.get("promptTokenCount")), _count(usage.get("candidatesTokenCount")), model


# ---- mcp-text ---------------------------------------------------------------------------------

def _build_mcp(messages, params):
    """A model behind an MCP tool takes one prompt: strict tool schemas refuse anything else, so the roles
    are folded into the text and no sampling arguments are sent."""
    system, rest = _split(messages)
    parts = ([system] if system else []) + [str(m.get("content") or "") for m in rest]
    return {"prompt": "\n\n".join(parts)}


def _parse_mcp(data):
    try:
        if data.get("isError"):
            raise ReplyShape("the MCP tool reported an error result")
        text = _blocks_text(data["content"])
    except (KeyError, TypeError, AttributeError):
        raise ReplyShape("no content[] text blocks in the MCP tool result")
    return text, None, None, None


def _bearer(key):
    return {"Authorization": "Bearer " + key} if key else {}


class Adapter(object):
    def __init__(self, name, build, select, input_names, parse, supports, schema_input=None, direct=False,
                 path=None, headers=_bearer, model_in_path=False, parse_full=None):
        self.name, self.build, self.select, self.parse = name, build, select, parse
        self.parse_full = parse_full
        self.input_names = frozenset(input_names)
        self.supports = dict({"json_schema": False, "tools": False, "stream": False}, **supports)
        self.schema_input, self.direct, self.model_in_path = schema_input, bool(direct), model_in_path
        self._path, self.headers = path, headers

    def path(self, engine):
        """-> the URL path under the engine's base_url ('' when this API has no direct support)."""
        if not self.direct or not self._path:
            return ""
        from urllib.parse import quote
        return self._path.format(model=quote(str(engine.get("model") or ""), safe=""))


ADAPTERS = {a.name: a for a in (
    Adapter("openai-chat", _build_openai, "choices[].message.content,choices[].finish_reason,usage,model", ("messages",), _parse_openai,
            {"json_schema": True, "tools": True, "stream": True}, schema_input="response_format", direct=True,
            path="/chat/completions", parse_full=_full_openai),
    Adapter("anthropic-messages", _build_anthropic, "content[].text,stop_reason,usage,model", ("messages",), _parse_anthropic,
            {"tools": False, "stream": True}, direct=True, path="/v1/messages",
            headers=lambda key: dict({"anthropic-version": "2023-06-01"}, **({"x-api-key": key} if key else {}))),
    Adapter("gemini-generate", _build_gemini, "candidates[].content.parts[].text,candidates[].finishReason,usageMetadata,modelVersion",
            ("contents",), _parse_gemini, {"json_schema": True, "tools": False, "stream": True},
            schema_input="generationConfig", direct=True, path="/models/{model}:generateContent",
            headers=lambda key: {"x-goog-api-key": key} if key else {}, model_in_path=True),
    Adapter("mcp-text", _build_mcp, "content,isError", ("prompt",), _parse_mcp, {}),
)}


# ---- why the engine stopped -------------------------------------------------------------------

_LENGTH = {"length", "max_tokens", "max_output_tokens", "model_length"}


def finish_of(api, resp):
    """-> "length" when the engine stopped because it ran out of completion tokens, "stop" when it ended on
    its own, any other reason lower-cased as the API names it, or None when the reply does not say. Never
    raises. A reasoning engine can spend the whole cap thinking and answer nothing: this is how the farm
    tells a cut reply from a finished one."""
    try:
        if api == "anthropic-messages":
            raw = resp.get("stop_reason")
        elif api == "gemini-generate":
            raw = resp["candidates"][0].get("finishReason")
        elif api == "openai-chat" or not api:
            raw = resp["choices"][0].get("finish_reason")
        else:
            return None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip().lower()
    if raw in _LENGTH:
        return "length"
    if raw in ("stop", "end_turn", "stop_sequence"):
        return "stop"
    return raw


def get(api):
    """-> the adapter for a row's `api` ('' or None is the default). An unknown name is refused, naming it and
    the names that exist."""
    name = (api or DEFAULT_API).strip().lower() if isinstance(api, str) or api is None else str(api)
    try:
        return ADAPTERS[name or DEFAULT_API]
    except KeyError:
        raise UnknownApi("unknown api %r; the farm speaks %s" % (name, ", ".join(sorted(ADAPTERS))))


def agent_capable(api):
    """-> True when the farm can run this API as a tool-using agent: the adapter says it supports tools AND it can
    parse a whole reply (parse_full). UnknownApi for a name the table does not have."""
    a = get(api)
    return bool(a.supports.get("tools")) and callable(a.parse_full)
