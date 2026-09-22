"""
Pluggable stress runner engine registry and exports.
"""

from typing import Dict, List, Type

from .base import StressRunner, TestEventListener, parse_time
from .prime95 import FFT_PRESETS, FFTConfig, Prime95Runner

_RUNNER_REGISTRY: Dict[str, Type[StressRunner]] = {
    "prime95": Prime95Runner,
}


def get_runner(name: str = "prime95", **kwargs) -> StressRunner:
    """Instantiates a stress runner by its identifier name."""
    name_lower = name.lower()
    if name_lower not in _RUNNER_REGISTRY:
        available = list(_RUNNER_REGISTRY.keys())
        raise ValueError(f"Unknown runner '{name}'. Available: {available}")
    return _RUNNER_REGISTRY[name_lower](**kwargs)


def list_runners() -> List[str]:
    """Returns a list of registered runner engine names."""
    return list(_RUNNER_REGISTRY.keys())


__all__ = [
    "StressRunner",
    "TestEventListener",
    "Prime95Runner",
    "FFTConfig",
    "FFT_PRESETS",
    "parse_time",
    "get_runner",
    "list_runners",
]

