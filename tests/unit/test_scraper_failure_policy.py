from src.scraper import _should_pause_immediately


def test_risk_control_failures_pause_immediately():
    assert _should_pause_immediately("baxia-dialog") is True
    assert _should_pause_immediately("J_MIDDLEWARE_FRAME_WIDGET") is True
    assert _should_pause_immediately("FAIL_SYS_USER_VALIDATE") is True


def test_transient_failures_do_not_pause_immediately():
    assert _should_pause_immediately("TimeoutError: response timeout") is False
