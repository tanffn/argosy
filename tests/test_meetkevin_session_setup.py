import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "setup_meetkevin_session.py"
    spec = importlib.util.spec_from_file_location("meetkevin_session_setup", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_only_app_origin_is_inspected():
    setup = module()
    class Page:
        url = "https://accounts.google.com/signin"
        def evaluate(self, *_):
            raise AssertionError("Must not read identity-provider session")
    assert setup.capture_session(Page(), None) is None


def test_no_session_does_not_read_cookies():
    setup = module()
    class Page:
        url = setup.APP_URL
        def evaluate(self, script):
            assert script == "sessionStorage.getItem('access_token')"
            return None
    assert setup.capture_session(Page(), None) is None


def test_captures_only_app_api_cookies():
    setup = module()
    class Page:
        url = setup.APP_URL
        def evaluate(self, _):
            return "test-token-not-real"
    class Context:
        def cookies(self, urls):
            assert urls == [setup.APP_URL, setup.API_URL]
            return [{"name": "test", "value": "test-cookie"}]
    result = setup.capture_session(Page(), Context())
    assert result["access_token"] == "test-token-not-real"
    assert result["origin"] == "https://app.meetkevin.com"
    assert result["captured_at"]
