import json
import pathlib
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def smart_home_payload():
    return json.loads((FIXTURES / "smart_home.json").read_text())["data"]


@pytest.fixture(autouse=True)
def tracked_sequences(request):
    """Most tests: room sequences run as tracked tasks so `async_block_till_done` waits for them.

    Tests marked `background_sequences` keep the real untracked background tasks.
    """
    if request.node.get_closest_marker("background_sequences"):
        yield
        return
    from unittest.mock import patch

    from custom_components.msheireb.coordinator import MsheirebCoordinator

    def _spawn(self, coro, name):
        return self.config_entry.async_create_task(self.hass, coro, name, eager_start=False)

    with patch.object(MsheirebCoordinator, "_spawn", _spawn), \
         patch.object(MsheirebCoordinator, "wait_for_sequences", True):
        yield


@pytest.fixture(autouse=True)
def instant_sequencing(request):
    """Legacy tests: no real power->fan waits and single-read fan confirmation.

    Tests marked `real_sequencing` keep the production values (10 s power settle, 2 s power polls, 5 s delay,
    5 s verify, 2 reads) and drive time themselves.
    """
    if request.node.get_closest_marker("real_sequencing"):
        yield
        return
    from unittest.mock import patch

    import custom_components.msheireb.climate as cl
    import custom_components.msheireb.coordinator as co

    with patch.object(cl, "TEMP_DEBOUNCE", 0), patch.object(co, "POWER_POLL_INTERVAL", 0), patch.object(co, "POWER_CONFIRM_MAX", 0.05), \
         patch.object(co, "DEFAULT_POWER_FAN_DELAY", 0), patch.object(co, "DEFAULT_POWER_SETTLE", 0), patch.object(co, "DEFAULT_FAN_SETTLE", 0), patch.object(co, "FAN_VERIFY_DELAY", 0), \
         patch.object(co, "FAN_VERIFY_READS", 1):
        yield


def pytest_configure(config):
    config.addinivalue_line("markers", "real_sequencing: use production power/fan timing constants")
    config.addinivalue_line("markers", "background_sequences: real (untracked) background sequences")
