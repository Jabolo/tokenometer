from __future__ import annotations
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    pass


def load_config(path: Path | None = None) -> dict:
    config = {'timezone': 'Europe/Warsaw', 'codex_roots': [], 'antigravity_roots': []}
    if path is not None:
        try:
            supplied = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ConfigError('Configuration must be a readable JSON object') from None
        if not isinstance(supplied, dict):
            raise ConfigError('Configuration must be a JSON object')
        unknown = set(supplied) - set(config)
        if unknown:
            raise ConfigError('Unsupported configuration keys')
        config.update(supplied)
    try:
        ZoneInfo(config['timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ConfigError('Timezone must be an installed IANA timezone') from None
    for key in ('codex_roots', 'antigravity_roots'):
        if not isinstance(config[key], list) or any(not isinstance(v, str) or not v.strip() for v in config[key]):
            raise ConfigError('History roots must be arrays of non-empty paths')
    return config


def resolve_roots(values):
    return sorted({Path(v).expanduser().resolve() for v in values}, key=str)
