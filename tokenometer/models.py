from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from hashlib import sha256
import re


def digest(value: str) -> str:
    return sha256(value.encode('utf-8', errors='replace')).hexdigest()


def utc_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        return date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class UsageEvent:
    key: str
    platform: str
    session_key: str
    timestamp: str | None
    fresh_input: int
    cache_read: int
    cache_write: int
    output: int
    reasoning: int | None = None
    model: str | None = None
    source_kind: str = 'recorded'
    counter_receipt: dict = field(default_factory=dict)
    session_aliases: tuple[str, ...] = ()

    def __post_init__(self):
        if self.platform not in ('codex', 'antigravity'):
            raise ValueError('Unknown platform')
        for name in ('fresh_input', 'cache_read', 'cache_write', 'output'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError('Usage counters must be non-negative integers')
        if self.reasoning is not None and (type(self.reasoning) is not int or not 0 <= self.reasoning <= self.output):
            raise ValueError('Reasoning must be part of output')
        if not re.fullmatch(self.platform + r':[a-f0-9]{64}', self.key):
            raise ValueError('Event identity must be platform-scoped SHA-256')
        if not re.fullmatch(r'[a-f0-9]{64}', self.session_key):
            raise ValueError('Session identity must be SHA-256')
        aliases = tuple(sorted(set(self.session_aliases or (self.session_key,))))
        if self.session_key not in aliases or any(not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{64}', v) for v in aliases):
            raise ValueError('Source session aliases must include the primary SHA-256 identity')
        object.__setattr__(self, 'session_aliases', aliases)
        if self.timestamp is not None and utc_timestamp(self.timestamp) is None:
            raise ValueError('Timestamp requires explicit timezone')

    @property
    def total(self) -> int:
        return self.fresh_input + self.cache_read + self.cache_write + self.output

    def to_dict(self):
        return asdict(self)


@dataclass
class CollectionResult:
    events: list[UsageEvent] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
