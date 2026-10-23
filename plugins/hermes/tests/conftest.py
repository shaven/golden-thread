import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).parent.parent / 'src'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

@pytest.fixture
def tmp_vault(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / 'Core.md').write_text("# Core Rules\n- Body trust is foundational.\n")
    (vault / 'Inbox.md').write_text("# Inbox\n")
    projects = vault / 'Projects' / 'demo'
    projects.mkdir(parents=True)
    (projects / 'idea.md').write_text("---\nstatus: active\n---\n# Demo project\n")
    (projects / 'CONVENTIONS.md').write_text("# Conventions\n- Use English.\n")
    return vault
