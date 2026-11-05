"""Тесты rv_moex.features: RV на синтетике, отсутствие утечки, неполные дни, гэпы."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from rv_moex import features as F


def make_day(day: str, rets: np.ndarray, start: str = "09:50", freq: str = "1min",
             p0: float = 100.0) -> pd.DataFrame:
    ts = pd.date_range(f"{day} {start}", periods=len(rets), freq=freq)
    close = p0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[p0], close[:-1]])
    return pd.DataFrame({
        "begin": ts, "end": ts + pd.Timedelta(freq) - pd.Timedelta(seconds=1),
        "open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close),
        "close": close, "value": 1e6, "volume": 1e3,
    })


def rv_reference(p0: float, rets_1min: np.ndarray) -> float:
    closes = p0 * np.exp(np.cumsum(rets_1min))
    logs = np.log(np.r_[p0, closes[9::10]])
    return float(np.sum(np.diff(logs) ** 2))


def test_rv_exact_for_constant_returns() -> None:
    d = 1e-4
    daily = F.compute_daily(make_day("2024-03-04", np.full(540, d)))
    assert daily["rv"].iloc[0] == pytest.approx(54 * (10 * d) ** 2, rel=1e-9)
    assert daily["log_rv"].iloc[0] == pytest.approx(np.log(54 * (10 * d) ** 2), rel=1e-9)
    assert bool(daily["is_complete"].iloc[0]) and daily["n_empty_bins"].iloc[0] == 0


def test_rv_matches_independent_reference() -> None:
    rng = np.random.default_rng(3)
    arrays = [rng.normal(0, 1e-3, 540) for _ in range(5)]
    days = pd.bdate_range("2024-03-04", periods=5)
    candles = pd.concat([make_day(d.strftime("%Y-%m-%d"), a) for d, a in zip(days, arrays)], ignore_index=True)
    daily = F.compute_daily(candles)
    expected = [rv_reference(100.0, a) for a in arrays]
    assert daily["rv"].to_numpy() == pytest.approx(expected, rel=1e-9)


def test_rv_matches_known_variance() -> None:
    rng = np.random.default_rng(7)
    sigma_day = 0.01
    days = pd.bdate_range("2023-01-02", periods=300)
    frames = [make_day(d.strftime("%Y-%m-%d"), rng.normal(0, sigma_day / np.sqrt(540), 540)) for d in days]
    daily = F.compute_daily(pd.concat(frames, ignore_index=True))
    assert daily["rv"].mean() == pytest.approx(sigma_day**2, rel=0.05)


def test_base_10_minutes_gives_sum_of_squares() -> None:
    rng = np.random.default_rng(5)
    rets = rng.normal(0, 2e-3, 54)
    daily = F.compute_daily(make_day("2024-03-04", rets, freq="10min"), F.FeatureConfig(base_minutes=10))
    assert daily["rv"].iloc[0] == pytest.approx(np.sum(rets**2), rel=1e-9)


def test_short_session_is_incomplete() -> None:
    rets = np.full(24, 1e-3)
    daily = F.compute_daily(make_day("2022-03-24", rets, freq="10min"), F.FeatureConfig(base_minutes=10))
    assert daily["n_empty_bins"].iloc[0] == 30
    assert not bool(daily["is_complete"].iloc[0])
    assert daily["rv"].iloc[0] == pytest.approx(24 * 1e-6, rel=1e-9)

def test_har_breaks_on_long_calendar_gap() -> None:
    idx = pd.DatetimeIndex(
        list(pd.bdate_range("2024-01-01", periods=10)) + list(pd.bdate_range("2024-03-01", periods=10)),
        name="date",
    )
    daily = pd.DataFrame({"rv": np.arange(1.0, 21.0), "is_complete": True}, index=idx)
    out = F.add_har_features(daily)
    assert out["rv_5"].iloc[4] == pytest.approx(3.0)
    assert out["rv_5"].iloc[10:14].isna().all()
    assert out["rv_5"].iloc[14] == pytest.approx(13.0)
    
@pytest.mark.parametrize("k", [5, 6])
def test_alpha_boundary(k: int) -> None:
    candles = make_day("2024-03-04", np.full(54, 1e-3), freq="10min")
    candles = candles.drop(index=range(10, 10 + k))
    daily = F.compute_daily(candles, F.FeatureConfig(base_minutes=10))
    assert daily["n_empty_bins"].iloc[0] == k
    assert bool(daily["is_complete"].iloc[0]) is (k == 5)


def test_weekend_sessions_are_excluded() -> None:
    frames = [make_day(d, np.full(54, 1e-3), freq="10min") for d in ("2024-03-08", "2024-03-09", "2024-03-11")]
    daily = F.compute_daily(pd.concat(frames, ignore_index=True), F.FeatureConfig(base_minutes=10))
    assert list(daily.index.strftime("%Y-%m-%d")) == ["2024-03-08", "2024-03-11"]


def test_gap_decomposition() -> None:
    z = np.zeros(54)
    frames = [
        make_day("2024-03-04", z, freq="10min", p0=100.0),
        make_day("2024-03-04", np.array([0.0]), start="19:00", freq="10min", p0=101.0),
        make_day("2024-03-05", np.array([0.0]), start="06:50", freq="10min", p0=102.0),
        make_day("2024-03-05", z, freq="10min", p0=103.0),
    ]
    daily = F.compute_daily(pd.concat(frames, ignore_index=True), F.FeatureConfig(base_minutes=10))
    row = daily.loc["2024-03-05"]
    assert row["gap_evening"] == pytest.approx(np.log(101 / 100))
    assert row["gap_morning"] == pytest.approx(np.log(102 / 101))
    assert row["gap_open"] == pytest.approx(np.log(103 / 102))
    assert row["gap"] == pytest.approx(np.log(1.03))
    assert row["gap_weekend"] == pytest.approx(0.0)
    assert row["gap"] == pytest.approx(row["gap_evening"] + row["gap_weekend"] + row["gap_morning"] + row["gap_open"])

def test_gap_weekend_component() -> None:
    z = np.zeros(54)
    frames = [
        make_day("2024-03-08", z, freq="10min", p0=100.0),
        make_day("2024-03-08", np.array([0.0]), start="19:00", freq="10min", p0=101.0),
        make_day("2024-03-09", z, freq="10min", p0=95.0),
        make_day("2024-03-10", z, freq="10min", p0=96.0),
        make_day("2024-03-11", np.array([0.0]), start="06:50", freq="10min", p0=97.0),
        make_day("2024-03-11", z, freq="10min", p0=98.0),
    ]
    daily = F.compute_daily(pd.concat(frames, ignore_index=True), F.FeatureConfig(base_minutes=10))
    assert list(daily.index.strftime("%Y-%m-%d")) == ["2024-03-08", "2024-03-11"]
    row = daily.loc["2024-03-11"]
    assert row["gap_evening"] == pytest.approx(np.log(101 / 100))
    assert row["gap_weekend"] == pytest.approx(np.log(96 / 101))
    assert row["gap_morning"] == pytest.approx(np.log(97 / 96))
    assert row["gap_open"] == pytest.approx(np.log(98 / 97))
    assert row["gap"] == pytest.approx(np.log(98 / 100))
    
def _candles(n: int = 40, seed: int = 1) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-01-08", periods=n)
    frames = [make_day(d.strftime("%Y-%m-%d"), rng.normal(0, 1e-3, 540)) for d in days]
    return pd.concat(frames, ignore_index=True), days


def _features(candles: pd.DataFrame) -> pd.DataFrame:
    return F.add_har_features(F.compute_daily(candles))


def test_no_lookahead_truncation() -> None:
    candles, days = _candles()
    cut = days[30]
    full = _features(candles)
    part = _features(candles[candles["begin"] < cut + pd.Timedelta(days=1)])
    pd.testing.assert_frame_equal(full.loc[:cut], part, check_freq=False)


def test_no_lookahead_future_perturbation() -> None:
    candles, days = _candles()
    cut = days[30]
    rng = np.random.default_rng(11)
    future = (candles["begin"] >= cut + pd.Timedelta(days=1)).to_numpy()
    cols = ["open", "high", "low", "close"]
    bumped = candles.copy()
    bumped.loc[future, cols] = bumped.loc[future, cols].to_numpy() * rng.uniform(0.5, 1.5, (future.sum(), 4))
    pd.testing.assert_frame_equal(_features(candles).loc[:cut], _features(bumped).loc[:cut], check_freq=False)


def test_har_windows_and_incomplete_days() -> None:
    idx = pd.date_range("2024-01-01", periods=30, freq="B", name="date")
    daily = pd.DataFrame({"rv": np.arange(1.0, 31.0), "is_complete": True}, index=idx)
    daily.loc[idx[10], "is_complete"] = False
    out = F.add_har_features(daily)
    assert out["rv_5"].iloc[4] == pytest.approx(3.0)
    assert out["rv_22"].iloc[21] != out["rv_22"].iloc[21]
    assert out["rv_1"].iloc[10] != out["rv_1"].iloc[10]
    assert out["rv_5"].iloc[10:15].isna().all() and out["rv_5"].iloc[15] == pytest.approx(np.mean([14, 15, 16, 17, 18.0]) + 0.0, abs=2.0)
    changed = daily.copy()
    changed.loc[idx[20:], "rv"] = 1e9
    assert F.add_har_features(changed)["rv_5"].iloc[:20].equals(out["rv_5"].iloc[:20])