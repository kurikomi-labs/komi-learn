"""komi-learn — a continuous, zero-friction learning layer for AI agents."""

# THE single source of truth for the version. pyproject.toml reads this literal at
# build time via setuptools dynamic version ([tool.setuptools.dynamic] version =
# {attr = "komi.__version__"}), so there is exactly one place to bump on a release
# and the packaged metadata can never drift from this value by construction.
__version__ = "0.5.0"

# When installed, prefer the distribution metadata — it's the ground truth of what
# pip actually has on disk, which is what `komi-learn update` compares against
# PyPI. For a bare source tree (no installed dist) the literal above stands in.
# Because of the build-time attr binding the two agree, so this only matters for
# odd editable-install states — and it can only correct toward reality, never
# introduce a second hand-maintained number.
try:  # importlib.metadata is stdlib on py3.8+
    from importlib.metadata import PackageNotFoundError, version as _dist_version

    try:
        __version__ = _dist_version("komi-learn")
    except PackageNotFoundError:
        pass  # not installed (source tree) — keep the literal
except Exception:  # pragma: no cover - metadata API should always be present
    pass
