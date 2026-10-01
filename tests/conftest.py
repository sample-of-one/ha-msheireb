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
