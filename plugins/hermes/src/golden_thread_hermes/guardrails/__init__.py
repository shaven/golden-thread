"""Golden Thread Observer Hooks guardrail plugin for Hermes.

This module implements the `register(ctx)` contract expected by
`hermes_agent.plugins` general plugins. It enforces Golden Thread Core
rules (body trust, anti-diet, anti-weight-stigma) by registering three
Observer Hooks:

- pre_llm_call: inject Core context when the user message touches
  body trust / health / weight / diet-culture topics.
- pre_tool_call: block dangerous shell commands and tool calls that
  would log/judge food or bodies.
- transform_llm_output: flag diet-culture language in the assistant
  response.
"""

from typing import Any, Callable, Dict, List, Optional

__all__ = ["register"]

# Trigger phrases for Core-context injection.
_BODY_TRUST_TRIGGERS = [
    "weight", "diet", "calories", "bmi", "body fat", "lose weight",
    "gain weight", "burn off", "cheat day", "clean eating", " macros ",
    "good food", "bad food", "feel fat", "hate my body", "my body is wrong",
]

# Tool / command patterns we refuse to run.
_DANGEROUS_COMMAND_PATTERNS = [
    "rm -rf /", "rm -rf /*", "mkfs.", "dd if=/dev/zero of=/dev/",
    ":(){ :|: ", "> /dev/", "shutdown", "reboot -f", "init 0",
]

_FOOD_SURVEILLANCE_TOOL_NAMES = [
    "myfitnesspal", "loseit", "cronometer", "noom", "weightwatchers",
]

_FOOD_SURVEILLANCE_CODE_PATTERNS = [
    "log calories", "calorie tracker", "track food", "track meal",
    "weight watchers", "myfitnesspal", "noom", "points",
]

_DIET_CULTURE_OUTPUT_PATTERNS = [
    "cheat day", "being bad", "good food", "bad food", "clean eating",
    "burn off", "earn your food", "guilty pleasure", "sinful food",
    "treat yourself, you deserve it", "new year new me", "summer body",
    "bikini body", "problem areas", "flattering", "skinny", "diet culture",
    "was so bad", "ate cake", "guilty", "bad today", "good today",
]


def _high_stakes(text: str) -> Optional[str]:
    """Return Core context to inject when user message touches body-trust topics."""
    if not text:
        return None
    lowered = text.lower()
    if any(trigger in lowered for trigger in _BODY_TRUST_TRIGGERS):
        return (
            "Golden Thread Core is active. "
            "Body trust is foundational; food has no moral valence; "
            "weight stigma and diet culture are harmful. "
            "Respond from body-trust, harm-reduction, and autonomy principles."
        )
    return None


def _is_dangerous_tool(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """Block shell commands or tools that could erase data or surveil bodies/food."""
    text = " ".join(str(v).replace("_", " ") for v in args.values()).lower()

    # Dangerous shell commands
    if tool_name in ("terminal", "shell", "bash", "execute_code"):
        for pattern in _DANGEROUS_COMMAND_PATTERNS:
            if pattern in text or pattern.replace(" ", "") in text:
                return f"Blocked dangerous command pattern: {pattern!r}"

    # Diet/food surveillance tools
    if any(name in tool_name.lower() for name in _FOOD_SURVEILLANCE_TOOL_NAMES):
        return f"Blocked body-surveillance tool: {tool_name}"
    for pattern in _FOOD_SURVEILLANCE_CODE_PATTERNS:
        if pattern in text:
            return f"Blocked body-surveillance activity: {pattern!r}"

    return None


def _maybe_violates_core(text: str) -> Optional[str]:
    """Flag diet-culture language in assistant output."""
    if not text:
        return None
    lowered = text.lower()
    for pattern in _DIET_CULTURE_OUTPUT_PATTERNS:
        if pattern in lowered:
            note = (
                "\n\n[Golden Thread guardrail note: this message contains "
                "diet-culture or body-shaming language. Food has no moral "
                "valence, and all bodies deserve respect.]"
            )
            return note
    return None


# ---------------------------------------------------------------------------
# Hook callbacks (match the Hermes invoke_hook() signatures)
# ---------------------------------------------------------------------------

def _pre_llm_call(
    *,
    session_id: str,
    user_message: str,
    conversation_history: List[Dict[str, Any]],
    is_first_turn: bool,
    model: str,
    platform: str,
    task_id: str = "",
    turn_id: str = "",
    sender_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Inject Core context into the user message when high-stakes topics appear."""
    core = _high_stakes(user_message)
    if core:
        return {"context": core}
    return None


def _pre_tool_call(
    *,
    tool_name: str,
    args: Dict[str, Any],
    session_id: str,
    task_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Block dangerous or body-surveillance tool calls."""
    reason = _is_dangerous_tool(tool_name, args)
    if reason:
        return {"block": True, "reason": reason}
    return None


def _transform_llm_output(
    *,
    response_text: str,
    session_id: str,
    model: str,
    platform: str,
    **kwargs: Any,
) -> Optional[str]:
    """Append a guardrail note when assistant output uses diet-culture language."""
    note = _maybe_violates_core(response_text)
    if note:
        return response_text + note
    return None


def register(ctx) -> None:
    """Hermes general-plugin entry point.

    Expected shape: module path in `hermes_agent.plugins` entry point,
    module exposes this top-level `register(ctx)` function.
    """
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
