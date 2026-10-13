"""Pure provider-notice parsing contracts; no browser or model calls."""

import pytest

from visibility_runner.websites.usage_limits import (
    parse_chatgpt_usage_limit_notice,
    parse_usage_limit_reset_time,
)


def test_tools_notice_preserves_display_clock_and_explicitly_allows_text():
    notice = (
        "Files, images, and data analysis are unavailable until usage resets "
        "tomorrow at 2:00\u202fAM. Continue chatting with text only, or upgrade "
        "for more access."
    )
    assert parse_chatgpt_usage_limit_notice(notice) == {
        "notice_text": notice,
        "scope": "tools",
        "text_allowed": True,
        "blocking": False,
        "reset_day": "tomorrow",
        "reset_clock": "02:00",
        "reset_display": "tomorrow at 2:00\u202fAM",
        "reset_timezone": None,
    }


@pytest.mark.parametrize(
    ("clock", "normalized"),
    [("2am", "02:00"), ("2:00 AM", "02:00"), ("12am", "00:00"),
     ("12pm", "12:00"), ("2:35 pm", "14:35"), ("02:00", "02:00"),
     ("23:59", "23:59")],
)
def test_message_limit_accepts_valid_12_and_24_hour_reset_clocks(clock, normalized):
    result = parse_chatgpt_usage_limit_notice(
        f"You've reached your message limit. Please try again today at {clock}."
    )
    assert result["scope"] == "text"
    assert result["text_allowed"] is False
    assert result["blocking"] is True
    assert result["reset_day"] == "today"
    assert result["reset_clock"] == normalized
    assert result["reset_timezone"] is None


@pytest.mark.parametrize("clock", ["25am", "13pm", "2:78", "0am", "24:00", "2", "2:00:30"])
def test_invalid_or_ambiguous_reset_clocks_are_not_reported(clock):
    result = parse_chatgpt_usage_limit_notice(
        f"You've hit your usage limit. Usage resets tomorrow at {clock}."
    )
    assert result["reset_clock"] is None
    assert result["reset_day"] is None
    assert result["reset_display"] is None


@pytest.mark.parametrize(
    "notice",
    ["Retry at 2am", "Something went wrong. Try again at 2am.",
     "The assistant recommends an appointment tomorrow at 2:00 AM."],
)
def test_time_or_retry_without_limit_semantics_is_not_a_limit_notice(notice):
    assert parse_chatgpt_usage_limit_notice(notice) is None


def test_generic_limit_does_not_prove_text_is_blocked_or_use_an_unrelated_clock():
    result = parse_chatgpt_usage_limit_notice(
        "An appointment is at 2am. You've hit your limit. Please try again later."
    )
    assert result["scope"] == "unknown"
    assert result["text_allowed"] is None
    assert result["reset_clock"] is None


def test_explicit_text_permission_overrides_message_limit_wording():
    result = parse_chatgpt_usage_limit_notice(
        "You've hit your message limit for files and images. "
        "Continue chatting with text only. Usage resets at 2am tomorrow."
    )
    assert result["scope"] == "tools"
    assert result["text_allowed"] is True
    assert result["reset_day"] == "tomorrow"
    assert result["reset_clock"] == "02:00"


def test_tools_scope_alone_does_not_infer_text_permission():
    result = parse_chatgpt_usage_limit_notice("You've reached your image limit.")
    assert result["scope"] == "tools"
    assert result["text_allowed"] is None
    assert result["blocking"] is False


def test_original_generic_limit_rejection_blocks_without_inventing_scope():
    result = parse_chatgpt_usage_limit_notice(
        "You've hit your limit. Please try again later."
    )
    assert result["scope"] == "unknown"
    assert result["text_allowed"] is None
    assert result["blocking"] is True
    assert result["reset_clock"] is None


@pytest.mark.parametrize('clock, expected', [('1:00 AM', '01:00'), ('1:00am', '01:00'), ('25:78am', None)])
def test_copilot_daily_limit_check_back_preserves_only_a_valid_display_clock(clock, expected):
    result = parse_chatgpt_usage_limit_notice(
        f"You've reached your daily limit. Get more usage now or check back at {clock}.")
    assert result['blocking'] is True and result['scope'] == 'unknown'
    assert result['reset_clock'] == expected
    assert result['reset_timezone'] is None and result['reset_day'] is None


def test_generic_limit_without_try_again_does_not_prove_blocking():
    result = parse_chatgpt_usage_limit_notice("You've exceeded your usage limit.")
    assert result["scope"] == "unknown"
    assert result["blocking"] is False


def test_explicit_text_permission_prevents_generic_limit_hold_without_tool_words():
    result = parse_chatgpt_usage_limit_notice(
        "You've hit your limit. Try again later. Continue chatting with text only."
    )
    assert result["scope"] == "unknown"
    assert result["text_allowed"] is True
    assert result["blocking"] is False


def test_shared_reset_display_validation_preserves_unicode_spacing():
    display = "tomorrow at 2:00\u202fAM"
    assert parse_usage_limit_reset_time(display) == {
        "reset_day": "tomorrow",
        "reset_clock": "02:00",
        "reset_display": display,
        "reset_timezone": None,
    }
    assert parse_usage_limit_reset_time("tomorrow at 13pm") is None
    assert parse_usage_limit_reset_time("tomorrow at 2am today") is None
    assert parse_usage_limit_reset_time("arbitrary prose at 2am") is None


def test_negative_text_permission_is_not_positive_permission():
    result = parse_chatgpt_usage_limit_notice(
        "You've reached your message limit. You cannot continue chatting with text."
    )
    assert result["scope"] == "text"
    assert result["text_allowed"] is False


def test_preserves_original_whitespace_but_bounds_notice_size():
    notice = "  You've\nreached your usage\u00a0limit. Usage resets at 2am.  "
    result = parse_chatgpt_usage_limit_notice(notice)
    assert result["notice_text"] == notice
    assert result["reset_clock"] == "02:00"
    assert parse_chatgpt_usage_limit_notice("You've hit your limit." + " " * 2000) is None
