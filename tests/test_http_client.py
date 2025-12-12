"""Tests for shared HTTP client."""

import pytest
from unittest.mock import patch, MagicMock

from utils.http_client import get_json, post_json, delete, DEFAULT_TIMEOUT


class TestHttpClient:
    """Test HTTP client wrapper functions."""

    def test_default_timeout_is_tuple(self):
        """Timeout should be (connect, read) tuple."""
        assert isinstance(DEFAULT_TIMEOUT, tuple)
        assert len(DEFAULT_TIMEOUT) == 2
        assert DEFAULT_TIMEOUT[0] > 0  # connect timeout
        assert DEFAULT_TIMEOUT[1] > 0  # read timeout

    @patch("utils.http_client.session")
    def test_get_json_parses_response(self, mock_session):
        """get_json should parse JSON response."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "value"}
        mock_resp.raise_for_status = MagicMock()
        mock_session.request.return_value = mock_resp

        result = get_json("https://example.com/api")

        assert result == {"key": "value"}
        mock_session.request.assert_called_once()
        mock_resp.raise_for_status.assert_called_once()

    @patch("utils.http_client.session")
    def test_post_json_sends_data(self, mock_session):
        """post_json should send JSON payload."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 123}
        mock_resp.raise_for_status = MagicMock()
        mock_session.request.return_value = mock_resp

        result = post_json("https://example.com/api", {"foo": "bar"})

        assert result == {"id": 123}
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["json"] == {"foo": "bar"}

    @patch("utils.http_client.session")
    def test_delete_calls_session(self, mock_session):
        """delete should call session with DELETE method."""
        mock_resp = MagicMock()
        mock_session.request.return_value = mock_resp

        result = delete("https://example.com/api/123")

        assert result == mock_resp
        call_args = mock_session.request.call_args
        assert call_args.kwargs.get("method") == "DELETE" or call_args[1].get("method") == "DELETE"

    @patch("utils.http_client.session")
    def test_custom_timeout_is_passed(self, mock_session):
        """Custom timeout should override default."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        mock_session.request.return_value = mock_resp

        get_json("https://example.com/api", timeout=30)

        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["timeout"] == 30
