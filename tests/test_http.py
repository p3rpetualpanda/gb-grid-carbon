import pytest
import responses

from gridcarbon.http import USER_AGENT, ApiError, RetryableHTTPError

URL = "https://example.test/data"


@responses.activate
def test_retries_transient_errors_then_succeeds(client):
    responses.add(responses.GET, URL, status=503)
    responses.add(responses.GET, URL, status=429)
    responses.add(responses.GET, URL, json={"ok": True})
    assert client.get_json(URL) == {"ok": True}
    assert len(responses.calls) == 3


@responses.activate
def test_gives_up_after_max_attempts(client):
    responses.add(responses.GET, URL, status=502)
    with pytest.raises(RetryableHTTPError):
        client.get_json(URL)
    assert len(responses.calls) == client.max_attempts


@responses.activate
def test_does_not_retry_client_errors(client):
    responses.add(responses.GET, URL, status=400, json={"error": "range greater than 31 days"})
    with pytest.raises(ApiError, match="31 days"):
        client.get_json(URL)
    assert len(responses.calls) == 1


@responses.activate
def test_sends_descriptive_user_agent(client):
    responses.add(responses.GET, URL, json={})
    client.get_json(URL)
    assert responses.calls[0].request.headers["User-Agent"] == USER_AGENT
