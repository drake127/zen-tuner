"""
Pluggable stress runner engine registry and exports.
"""

from typing import Dict, List, Set, Type

from .base import StressRunner, TestEventListener, parse_step_time, parse_time
from .prime95 import FFT_PRESET_MATRIX, FFT_PRESETS, FFTConfig, Prime95Runner, build_fft_config
from .y_cruncher import ALGORITHM_PRESETS, YCruncherRunner

_RUNNER_REGISTRY: Dict[str, Type[StressRunner]] = {
    "prime95": Prime95Runner,
    "y-cruncher": YCruncherRunner,
    "ycruncher": YCruncherRunner,
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


def get_registered_runner_classes() -> List[Type[StressRunner]]:
    """Returns distinct StressRunner classes in registry."""
    seen: Set[Type[StressRunner]] = set()
    result: List[Type[StressRunner]] = []
    for cls in _RUNNER_REGISTRY.values():
        if cls not in seen:
            seen.add(cls)
            result.append(cls)
    return result


__all__ = [
    "StressRunner",
    "TestEventListener",
    "Prime95Runner",
    "YCruncherRunner",
    "FFTConfig",
    "FFT_PRESETS",
    "FFT_PRESET_MATRIX",
    "ALGORITHM_PRESETS",
    "build_fft_config",
    "parse_time",
    "parse_step_time",
    "get_runner",
    "list_runners",
    "get_registered_runner_classes",
]

