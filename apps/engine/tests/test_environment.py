"""Smoke tests for the engine environment.

These are deliberately thin: P0-01 has no logic to test. They exist to fail
loudly if the interpreter floor or the workspace packaging breaks, which is
the only thing that can be wrong at this stage.
"""

import sys

import engine


def test_python_version_meets_floor() -> None:
    assert sys.version_info >= (3, 12), f"requires Python 3.12+, got {sys.version}"


def test_engine_package_is_importable() -> None:
    assert engine.__version__ == "0.0.0"
