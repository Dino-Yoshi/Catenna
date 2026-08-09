"""Tests for the mock pipeline package are driven by `mock-test`."""

import os

# Keep captured-stdout assertions independent from the caller's shell.
os.environ.pop("FORCE_COLOR", None)
