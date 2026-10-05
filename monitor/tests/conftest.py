import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flowmon import config as config_mod  # noqa: E402


def load_raw() -> dict:
    return tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))


def make_cfg(tmp_path: Path, **sections) -> config_mod.Config:
    """以样例配置为底，按段覆盖若干项。make_cfg(tmp, score={"baseline_hours": 0.1})"""
    raw = load_raw()
    for sec, kv in sections.items():
        raw[sec].update(kv)
    return config_mod.from_dict(raw, tmp_path)


@pytest.fixture
def cfg_factory(tmp_path):
    def f(**sections):
        return make_cfg(tmp_path, **sections)
    return f
