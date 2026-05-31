"""komi-learn — a continuous, zero-friction learning layer for AI agents."""

# Single source of truth is the distribution metadata (pyproject's version). We
# read it at runtime so we never drift from what's actually installed — which is
# exactly what `komi-learn update` compares against PyPI. The literal below is
# only a fallback for running straight from a source tree with no installed dist.
_FALLBACK_VERSION = "0.3.0"

try:  # py3.8+: importlib.metadata is stdlib
    from importlib.metadata import PackageNotFoundError, version as _dist_version

    try:
        __version__ = _dist_version("komi-learn")
    except PackageNotFoundError:
        __version__ = _FALLBACK_VERSION
except Exception:  # pragma: no cover - metadata API should always be present
    __version__ = _FALLBACK_VERSION
