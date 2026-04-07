"""Tests for retry-after parsing chain."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.client.workers.captions.retry_after import (
    parse_google_message_fallback,
    parse_google_retry_info,
    parse_retry_after,
    parse_retry_after_header,
)


def _mock_response(
    headers: dict | None = None,
    json_body: dict | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.headers = headers or {}
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


# ---------------------------------------------------------------------------
# parse_retry_after_header
# ---------------------------------------------------------------------------


def test_header_integer():
    resp = _mock_response(headers={"Retry-After": "5"})
    assert parse_retry_after_header(resp) == 5.0


def test_header_float():
    resp = _mock_response(headers={"Retry-After": "4.2"})
    assert parse_retry_after_header(resp) == 4.2


def test_header_missing():
    resp = _mock_response(headers={})
    assert parse_retry_after_header(resp) is None


def test_header_non_numeric():
    resp = _mock_response(headers={"Retry-After": "Thu, 01 Jan 2099 00:00:00 GMT"})
    assert parse_retry_after_header(resp) is None


# ---------------------------------------------------------------------------
# parse_google_retry_info
# ---------------------------------------------------------------------------


def test_google_retry_info():
    body = {
        "error": {
            "details": [
                {"@type": "type.googleapis.com/google.rpc.Help"},
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "4s",
                },
            ]
        }
    }
    resp = _mock_response(json_body=body)
    assert parse_google_retry_info(resp) == 4.0


def test_google_retry_info_fractional():
    body = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "32.5s",
                },
            ]
        }
    }
    resp = _mock_response(json_body=body)
    assert parse_google_retry_info(resp) == 32.5


def test_google_retry_info_exact_response():
    """Exact Google Gemini 429 response (array-wrapped)."""
    body = [{
        "error": {
            "code": 429,
            "message": (
                "You exceeded your current quota, please check your plan and billing details. "
                "For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. "
                "To monitor your current usage, head to: https://ai.dev/rate-limit. \n"
                "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                "limit: 10, model: gemini-2.5-flash-lite\n"
                "Please retry in 4.185577026s."
            ),
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.Help",
                    "links": [
                        {
                            "description": "Learn more about Gemini API quotas",
                            "url": "https://ai.google.dev/gemini-api/docs/rate-limits",
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                            "quotaDimensions": {"location": "global", "model": "gemini-2.5-flash-lite"},
                            "quotaValue": "10",
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "4s",
                },
            ],
        }
    }]
    resp = _mock_response(json_body=body)
    # Should find RetryInfo, not fall through to 60s default
    assert parse_retry_after(resp) == 5.0  # 4s + 1s buffer
    assert parse_google_retry_info(resp) == 4.0  # raw parser has no buffer


def test_google_retry_info_missing_type():
    body = {"error": {"details": [{"retryDelay": "4s"}]}}
    resp = _mock_response(json_body=body)
    assert parse_google_retry_info(resp) is None


def test_google_retry_info_no_details():
    body = {"error": {"message": "rate limited"}}
    resp = _mock_response(json_body=body)
    assert parse_google_retry_info(resp) is None


def test_google_retry_info_no_json():
    resp = _mock_response()
    assert parse_google_retry_info(resp) is None


# ---------------------------------------------------------------------------
# parse_google_message_fallback
# ---------------------------------------------------------------------------


def test_google_message_fallback():
    body = {
        "error": {
            "message": "Quota exceeded. Please retry in 4.185577026s.",
        }
    }
    resp = _mock_response(json_body=body)
    assert parse_google_message_fallback(resp) == pytest.approx(4.185577026)


def test_google_message_fallback_no_match():
    body = {"error": {"message": "Something went wrong"}}
    resp = _mock_response(json_body=body)
    assert parse_google_message_fallback(resp) is None


# ---------------------------------------------------------------------------
# parse_retry_after (full chain)
# ---------------------------------------------------------------------------


def test_chain_prefers_header():
    """Header wins even when Google body is also present."""
    body = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "30s",
                },
            ]
        }
    }
    resp = _mock_response(headers={"Retry-After": "5"}, json_body=body)
    assert parse_retry_after(resp) == 6.0  # 5 + 1s buffer


def test_chain_falls_through_to_google():
    body = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "4s",
                },
            ]
        }
    }
    resp = _mock_response(json_body=body)
    assert parse_retry_after(resp) == 5.0  # 4 + 1s buffer


def test_chain_falls_through_to_message():
    body = {"error": {"message": "Please retry in 2.5s."}}
    resp = _mock_response(json_body=body)
    assert parse_retry_after(resp) == 3.5  # 2.5 + 1s buffer


def test_chain_falls_through_to_default():
    """When no provider-specific hint is found, default to 60s + 1s buffer."""
    resp = _mock_response(json_body={"error": {"message": "unknown"}})
    assert parse_retry_after(resp) == 61.0
