"""Golden Thread integration for Hermes Agent.

Provides:
- A memory provider that exposes 52 Golden Thread commands as Hermes tools.
  All 39 canonical `/gt:gt-*` commands from the Claude Code plugin are present;
  the remaining 13 are Hermes-specific helpers, aliases, or guidance stubs for
  UI-only / external-subsystem commands that have no direct Hermes equivalent.
- Guardrails enforcing Golden Thread Core principles (body trust, anti-diet,
  anti-weight-stigma, harm reduction).
"""

from __future__ import annotations

from golden_thread_hermes.memory.provider import GoldenThreadMemoryProvider
from golden_thread_hermes.guardrails import register as register_guardrails

__all__ = ["GoldenThreadMemoryProvider", "register_guardrails"]
