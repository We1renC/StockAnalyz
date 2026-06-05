from learning.dsr_threshold import (
    dsr_probability,
    required_sharpe_for_dsr,
    update_confluence_threshold_by_dsr,
)


def test_dsr_probability_increases_with_sharpe():
    low = dsr_probability(0.2, 0.0, 40.0)
    high = dsr_probability(1.0, 0.0, 40.0)
    assert 0.0 <= low < high <= 1.0


def test_required_sharpe_for_dsr_hits_target_probability():
    required = required_sharpe_for_dsr(0.95, 0.0, 60.0)
    prob = dsr_probability(required, 0.0, 60.0)
    assert required > 0.0
    assert prob >= 0.949


def test_update_confluence_threshold_by_dsr_is_bounded_and_monotonic():
    raised = update_confluence_threshold_by_dsr(
        current_threshold=8.0,
        base_threshold=8.0,
        max_threshold=12.0,
        current_sr=0.4,
        required_sr=1.8,
        k=1.25,
        smoothing=0.25,
    )
    stable = update_confluence_threshold_by_dsr(
        current_threshold=8.0,
        base_threshold=8.0,
        max_threshold=12.0,
        current_sr=2.2,
        required_sr=1.8,
        k=1.25,
        smoothing=0.25,
    )

    assert 8.0 <= raised <= 12.0
    assert 8.0 <= stable <= 12.0
    assert raised > stable
    assert stable == 8.0
