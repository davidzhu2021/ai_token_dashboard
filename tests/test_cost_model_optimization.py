import pytest

from backend.main import _model_optimization_space


def test_model_optimization_space_is_zero_for_stable_spend() -> None:
    optimization, median = _model_optimization_space([100, 100, 100, 0])
    assert optimization == pytest.approx(0)
    assert median == pytest.approx(100)


def test_model_optimization_space_accumulates_peaks_above_median() -> None:
    optimization, median = _model_optimization_space([100, 100, 300, 0, 200])
    assert median == pytest.approx(150)
    assert optimization == pytest.approx(200)


@pytest.mark.parametrize("values", ([100, 200], [0, 0, 0], []))
def test_model_optimization_space_requires_three_spend_days(values) -> None:
    assert _model_optimization_space(values) == (0.0, None)


def test_model_optimization_space_ignores_zero_days_for_baseline() -> None:
    optimization, median = _model_optimization_space([0, 10, 20, 30, 0])
    assert median == pytest.approx(20)
    assert optimization == pytest.approx(10)
