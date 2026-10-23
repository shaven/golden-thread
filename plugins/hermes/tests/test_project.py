from golden_thread_hermes.memory.project import ProjectManager
from golden_thread_hermes.memory.vault import Vault

def test_log_and_decision(tmp_vault):
    v = Vault(str(tmp_vault))
    pm = ProjectManager(v, 'demo')
    pm.log('test entry')
    log_file = next((tmp_vault / 'Projects' / 'demo' / 'Log').glob('*.md'))
    assert 'test entry' in log_file.read_text()
    adr = pm.decide('ADR-001', 'we need one', 'pick X', 'simpler')
    assert adr == 'ADR-0001'
    assert 'pick X' in pm.read_context()

def test_open_sets_active(tmp_vault):
    v = Vault(str(tmp_vault))
    pm = ProjectManager(v)
    pm.open('demo')
    assert pm.active_slug == 'demo'

def test_create_and_activate(tmp_vault):
    v = Vault(str(tmp_vault))
    pm = ProjectManager(v)
    slug = pm.create('Other', 'second project')
    assert slug == 'other'
    assert pm.active_slug == 'other'
    assert (tmp_vault / 'Projects' / 'other' / 'idea.md').exists()
