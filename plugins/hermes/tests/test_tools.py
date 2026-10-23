import json
from unittest.mock import patch
from pathlib import Path

from golden_thread_hermes.memory.provider import GoldenThreadMemoryProvider
from golden_thread_hermes.memory.config import GoldenThreadConfig

def make_provider(vault):
    p = GoldenThreadMemoryProvider()
    cfg = GoldenThreadConfig(vault)
    with patch.object(GoldenThreadConfig, 'load', return_value=cfg):
        with patch.object(GoldenThreadConfig, 'save'):
            p.initialize('test-session')
    return p

def test_gt_list(tmp_vault):
    p = make_provider(tmp_vault)
    res = json.loads(p.handle_tool_call('gt_list', {}))
    assert res['projects'] == ['demo']

def test_gt_open(tmp_vault):
    p = make_provider(tmp_vault)
    res = json.loads(p.handle_tool_call('gt_open', {'project_slug': 'demo'}))
    assert res['active_project'] == 'demo'

def test_gt_create_and_work(tmp_vault):
    p = make_provider(tmp_vault)
    res = json.loads(p.handle_tool_call('gt_create', {'name': 'other', 'idea': 'second project'}))
    assert res['project'] == 'other'
    res2 = json.loads(p.handle_tool_call('gt_work', {'summary': 'added detail'}))
    assert res2['status'] == 'logged'
    assert res2['project'] == 'other'
    assert 'added detail' in (tmp_vault / 'Projects' / 'other' / 'Log').glob('*.md').__next__().read_text()

def test_gt_decide(tmp_vault):
    p = make_provider(tmp_vault)
    json.loads(p.handle_tool_call('gt_open', {'project_slug': 'demo'}))
    res = json.loads(p.handle_tool_call('gt_decide', {
        'title': 'ADR-001',
        'context': 'we need one',
        'decision': 'pick X',
        'consequences': 'simpler'
    }))
    assert res['adr'] == 'ADR-0001'
