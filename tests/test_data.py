"""Офлайн-тесты rv_moex.data: пагинация, повторы, кэш, контроль качества."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from rv_moex import data as D

COLS = ["open", "close", "high", "low", "value", "volume", "begin", "end"]


def make_frame(days: list[str]) -> pd.DataFrame:
    rows = []
    for d in days:
        for ts in pd.date_range(f"{d} 09:50", f"{d} 18:40", freq="10min"):
            c = 100.0 + len(rows) * 0.01
            rows.append({"begin": ts, "end": ts + pd.Timedelta(minutes=9, seconds=59),
                         "open": c, "high": c + 0.1, "low": c - 0.1, "close": c,
                         "value": 1e6, "volume": 1e3})
    return pd.DataFrame(rows)


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code, self._payload, self.url = status, payload, "fake://iss"

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, frame: pd.DataFrame, page: int = 4, fail_first: int = 0) -> None:
        self.frame, self.page, self.fail = frame, page, fail_first
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None):
        assert params is not None
        self.calls.append(dict(params))
        if self.fail > 0:
            self.fail -= 1
            return FakeResponse(503, {})
        lo, hi = pd.Timestamp(params["from"]), pd.Timestamp(params["till"])
        sub = self.frame[(self.frame["begin"] >= lo) & (self.frame["begin"] <= hi)]
        sub = sub.iloc[params["start"]: params["start"] + self.page]
        out = sub.assign(begin=sub["begin"].dt.strftime("%Y-%m-%d %H:%M:%S"),
                         end=sub["end"].dt.strftime("%Y-%m-%d %H:%M:%S"))
        return FakeResponse(200, {"candles": {"columns": COLS, "data": out[COLS].values.tolist()}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(D.time, "sleep", lambda _s: None)


FRAME = make_frame(["2024-06-10", "2024-06-11", "2024-06-13", "2024-06-14"])


def test_interval_5_rejected() -> None:
    with pytest.raises(ValueError):
        D.fetch_candles("SBER", 5, date(2024, 6, 10), date(2024, 6, 11), session=FakeSession(FRAME))


def test_pagination_collects_all_rows() -> None:
    out = D.fetch_candles("SBER", 10, date(2024, 6, 10), date(2024, 6, 14), session=FakeSession(FRAME))
    assert len(out) == len(FRAME)


def test_retry_on_503() -> None:
    s = FakeSession(FRAME, fail_first=2)
    out = D.fetch_candles("SBER", 10, date(2024, 6, 10), date(2024, 6, 11), session=s)
    assert len(out) == 108 and s.calls[0] == s.calls[1] == s.calls[2]


def test_clean_candles_drops_duplicates() -> None:
    dup = pd.concat([FRAME, FRAME.iloc[:5]], ignore_index=True)
    out, n = D.clean_candles(dup)
    assert n == 5 and len(out) == len(FRAME) and out["begin"].is_monotonic_increasing


def test_cache_is_incremental(tmp_path) -> None:
    s = FakeSession(FRAME)
    first = D.load_candles("SBER", 10, date(2024, 6, 10), date(2024, 6, 12), data_dir=tmp_path, session=s)
    n1 = len(s.calls)
    second = D.load_candles("SBER", 10, date(2024, 6, 10), date(2024, 6, 14), data_dir=tmp_path, session=s)
    assert len(first) == 108 and len(second) == len(FRAME)
    assert {c["from"] for c in s.calls[n1:]} == {"2024-06-13"}
    n2 = len(s.calls)
    D.load_candles("SBER", 10, date(2024, 6, 10), date(2024, 6, 14), data_dir=tmp_path, session=s)
    assert len(s.calls) == n2


def test_check_candles_counts_missing_slots_inside_session_only() -> None:
    df = make_frame(["2024-06-10"])
    evening = pd.DataFrame({"begin": [pd.Timestamp("2024-06-10 19:00")], "end": [pd.Timestamp("2024-06-10 19:09:59")],
                            "open": [101.0], "high": [101.1], "low": [100.9], "close": [101.0],
                            "value": [1e6], "volume": [1e3]})
    full = pd.concat([df, evening], ignore_index=True)
    assert D.check_candles(full, "SBER", 10).n_missing_slots == 0
    holed = full.drop(index=[10]).reset_index(drop=True)
    rep = D.check_candles(holed, "SBER", 10)
    assert rep.n_missing_slots == 1 and rep.n_duplicates == 0 and rep.n_bad_prices == 0