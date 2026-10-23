from golden_thread_hermes.guardrails import register, _high_stakes, _is_dangerous_tool, _maybe_violates_core

def test_core_context_injects_on_body_trust():
    assert _high_stakes('I hate my weight and want to diet') is not None

def test_core_context_no_injection():
    assert _high_stakes('What is the weather?') is None

def test_blocks_dangerous_shell():
    assert _is_dangerous_tool('terminal', {'command':'rm -rf /'}) is not None
    assert _is_dangerous_tool('terminal', {'command':'ls'}) is None

def test_blocks_diet_tool():
    assert _is_dangerous_tool('execute_code', {'code':'log calories to MyFitnessPal'}) is not None
    assert _is_dangerous_tool('web_search', {'query':'nutrition'}) is None

def test_flags_diet_language():
    assert _maybe_violates_core('Enjoy your cheat day!') is not None
    assert _maybe_violates_core('I was so bad today I ate cake.') is not None

def test_register_hooks():
    hooks = []
    class Ctx:
        def register_hook(self, name, fn):
            hooks.append(name)
    register(Ctx())
    assert set(hooks) == {'pre_llm_call', 'pre_tool_call', 'transform_llm_output'}
