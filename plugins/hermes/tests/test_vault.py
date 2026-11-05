from golden_thread_hermes.memory.vault import Vault

def test_vault_core_read(tmp_vault):
    v = Vault(str(tmp_vault))
    assert 'Body trust' in v.read_core()
    assert v.list_projects() == ['demo']

def test_slugify(tmp_vault):
    v = Vault(str(tmp_vault))
    assert v.slugify('Hello World!') == 'hello-world'
