import copy
from pathlib import Path

import yaml
import pytest

from core.modules.loader import load_manifest, ModuleManifestError
from core.modules.schema import ModuleManifest
from core.modules import loader as loader_mod


GOLDEN = Path(loader_mod.__file__).resolve().parent / 'examples' / 'scout.yaml'


def test_golden_loads():
    m = load_manifest(GOLDEN)
    assert isinstance(m, ModuleManifest)
    assert m.id == 'scout'
    assert m.verification_class == 'claim-ratchet'


@pytest.fixture
def base_manifest():
    return load_manifest(GOLDEN).model_dump()


def test_reject_io_contract(base_manifest, tmp_path):
    manifest = copy.deepcopy(base_manifest)
    manifest['faces'][0]['io_contract'] = 'tools_bogus'
    path = tmp_path / 'm.yaml'
    with open(path, 'w') as f:
        yaml.dump(manifest, f)
    with pytest.raises(ModuleManifestError, match='io_contract'):
        load_manifest(path)


def test_reject_tier(base_manifest, tmp_path):
    manifest = copy.deepcopy(base_manifest)
    manifest['tools'][0]['tier'] = 'Admin'
    path = tmp_path / 'm.yaml'
    with open(path, 'w') as f:
        yaml.dump(manifest, f)
    with pytest.raises(ModuleManifestError, match='tier'):
        load_manifest(path)


def test_reject_pipes(base_manifest, tmp_path):
    manifest = copy.deepcopy(base_manifest)
    manifest['pipes'] = ['chat', 'vision']
    path = tmp_path / 'm.yaml'
    with open(path, 'w') as f:
        yaml.dump(manifest, f)
    with pytest.raises(ModuleManifestError, match='pipes|arm'):
        load_manifest(path)


def test_reject_verification_class(base_manifest, tmp_path):
    manifest = copy.deepcopy(base_manifest)
    manifest['verification_class'] = 'vibes'
    path = tmp_path / 'm.yaml'
    with open(path, 'w') as f:
        yaml.dump(manifest, f)
    with pytest.raises(ModuleManifestError, match='verification_class'):
        load_manifest(path)


def test_reject_always_human(base_manifest, tmp_path):
    manifest = copy.deepcopy(base_manifest)
    manifest['tools'].append({'id': 'publish', 'tier': 'Full-Access'})
    manifest['always_human'] = []
    path = tmp_path / 'm.yaml'
    with open(path, 'w') as f:
        yaml.dump(manifest, f)
    with pytest.raises(ModuleManifestError, match='always_human'):
        load_manifest(path)
