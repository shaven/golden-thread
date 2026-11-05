import json
from unittest.mock import patch

import json
from golden_thread_hermes.memory.provider import GoldenThreadMemoryProvider
from golden_thread_hermes.memory.config import GoldenThreadConfig

def make_provider(vault):
    p = GoldenThreadMemoryProvider()
    cfg = GoldenThreadConfig(vault)
    with patch.object(GoldenThreadConfig, 'load', return_value=cfg):
        with patch.object(GoldenThreadConfig, 'save'):
            p.initialize('test-session')
    return p

def test_provider_lifecycle(tmp_vault):
    p = make_provider(tmp_vault)
    assert p.is_available()

def test_provider_schemas(tmp_vault):
    p = make_provider(tmp_vault)
    schemas = p.get_tool_schemas()
    names = sorted([s['name'] for s in schemas])
    assert len(names) == 52
    # All canonical Claude Code commands are present
    canonical = [
        'gt_allin', 'gt_allin_commit', 'gt_brief', 'gt_calibrate', 'gt_close',
        'gt_complete', 'gt_context', 'gt_create', 'gt_digest', 'gt_doctor',
        'gt_farm', 'gt_forget', 'gt_grant', 'gt_handle', 'gt_help',
        'gt_implement', 'gt_ingest', 'gt_init', 'gt_lint', 'gt_list',
        'gt_minimize', 'gt_open', 'gt_optimize', 'gt_plan', 'gt_promote',
        'gt_query', 'gt_refresh', 'gt_review', 'gt_route', 'gt_runbook_lint',
        'gt_scan', 'gt_settings', 'gt_setup', 'gt_sync', 'gt_upgrade',
        'gt_validate', 'gt_validation', 'gt_watch', 'gt_work',
    ]
    for name in canonical:
        assert name in names, f"missing canonical command {name}"

def test_provider_system_prompt(tmp_vault):
    p = make_provider(tmp_vault)
    # The provider always injects Core rules when a Core.md exists
    block = p.system_prompt_block()
    assert 'Body trust' in block
    # After opening a project, the prefetch returns project context
    json.loads(p.handle_tool_call('gt_open', {'project_slug': 'demo'}))
    prefetch = p.prefetch("status")
    assert 'demo' in prefetch
