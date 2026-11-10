import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shipment_agent import clock  # noqa: E402
from shipment_agent.tools import ActionTools, ReadTools, ToolContext  # noqa: E402

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(NOW)
    yield


@pytest.fixture
def ctx():
    return ToolContext()


@pytest.fixture
def read(ctx):
    return ReadTools(ctx)


@pytest.fixture
def act(ctx):
    return ActionTools(ctx)
