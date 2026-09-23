"""
Pluggable stress runner engine registry and exports.
"""

from .base import Engine, RunContext, RunnerUnavailableError, StressRunner, TestEventListener, parse_duration
from .prime95 import FFT_PRESETS, FFTConfig, Prime95Runner, build_fft_config
from .y_cruncher import ALGORITHM_PRESETS, YCruncherRunner

RUNNER_CLASSES: dict[str, type[StressRunner]] = {cls.name: cls for cls in (Prime95Runner, YCruncherRunner)}


def get_runner(name: str, **kwargs) -> StressRunner:
    """Instantiates a stress runner by its identifier name."""
    try:
        return RUNNER_CLASSES[name.lower()](**kwargs)
    except KeyError:
        raise ValueError(f"Unknown runner '{name}'. Available: {list(RUNNER_CLASSES)}") from None


__all__ = [
    "ALGORITHM_PRESETS",
    "Engine",
    "FFTConfig",
    "FFT_PRESETS",
    "Prime95Runner",
    "RUNNER_CLASSES",
    "RunContext",
    "RunnerUnavailableError",
    "StressRunner",
    "TestEventListener",
    "YCruncherRunner",
    "build_fft_config",
    "get_runner",
    "parse_duration",
]
