"""Реализованная волатильность, ночной гэп и признаки HAR по внутридневным свечам."""
from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from rv_moex.data import (
    DEFAULT_DATA_DIR, MAIN_END_MIN, MAIN_START_MIN, TICKERS, load_candles, session_label,
)

log = logging.getLogger(__name__)

MAIN_MINUTES = MAIN_END_MIN - MAIN_START_MIN
DAILY_COLUMNS = (
    "open", "close", "ret_day", "rv", "log_rv", "gap", "gap_evening", "gap_morning",
    "gap_open", "n_empty_bins", "n_bins", "is_complete",
)


@dataclass(frozen=True)
class FeatureConfig:
    """grid_minutes — шаг сетки RV; base_minutes — шаг исходных свечей; alpha — порог пустых корзин."""

    grid_minutes: int = 10
    base_minutes: int = 1
    alpha: float = 0.10
    har_windows: tuple[int, ...] = (1, 5, 22)

    def __post_init__(self) -> None:
        if MAIN_MINUTES % self.grid_minutes:
            raise ValueError("grid_minutes должен делить длину основной сессии (540 мин)")
        if self.grid_minutes % self.base_minutes:
            raise ValueError("grid_minutes должен быть кратен base_minutes")
        if not 0.0 <= self.alpha < 1.0:
            raise ValueError("alpha должен лежать в [0, 1)")

    @property
    def n_bins(self) -> int:
        return MAIN_MINUTES // self.grid_minutes


def _empty_daily() -> pd.DataFrame:
    out = pd.DataFrame({c: pd.Series(dtype="float64") for c in DAILY_COLUMNS})
    out["is_complete"] = out["is_complete"].astype(bool)
    out.index = pd.DatetimeIndex([], name="date")
    return out


def compute_daily(candles: pd.DataFrame, config: FeatureConfig | None = None) -> pd.DataFrame:
    """Дневные RV, гэпы и флаги полноты по свечам одного тикера (индекс — дата торгового дня)."""
    cfg = config or FeatureConfig()
    need = {"begin", "open", "close"}
    if not need <= set(candles.columns):
        raise ValueError(f"Нет колонок: {sorted(need - set(candles.columns))}")
    if candles.empty:
        return _empty_daily()

    df = candles.sort_values("begin").reset_index(drop=True)
    begin = df["begin"]
    minute = begin.dt.hour * 60 + begin.dt.minute
    df["date"] = begin.dt.normalize()
    df["session"] = session_label(begin)
    df["bin"] = (minute - MAIN_START_MIN) // cfg.grid_minutes
    df = df[begin.dt.day_of_week < 5]

    main = df[df["session"] == "main"]
    if main.empty:
        return _empty_daily()

    grid = main.groupby(["date", "bin"]).agg(open=("open", "first"), close=("close", "last"))
    days = grid.index.get_level_values("date").unique()
    full = pd.MultiIndex.from_product([days, range(cfg.n_bins)], names=["date", "bin"])
    grid = grid.reindex(full)

    by_day = grid.groupby(level="date")
    p0 = by_day["open"].transform("first")
    close_ff = by_day["close"].ffill().fillna(p0)
    log_c = np.log(close_ff)
    prev = log_c.groupby(level="date").shift(1).fillna(np.log(p0))
    ret = log_c - prev

    rv = (ret**2).groupby(level="date").sum()
    daily = pd.DataFrame({
        "open": p0.groupby(level="date").first(),
        "close": close_ff.groupby(level="date").last(),
        "ret_day": ret.groupby(level="date").sum(),
        "rv": rv,
        "n_empty_bins": grid["close"].isna().groupby(level="date").sum().astype(int),
    })
    daily["n_bins"] = cfg.n_bins
    daily["is_complete"] = daily["n_empty_bins"] <= cfg.alpha * cfg.n_bins + 1e-9
    daily["log_rv"] = np.log(daily["rv"].where((daily["rv"] > 0) & daily["is_complete"]))

    eve_last = df[df["session"] == "evening"].groupby("date")["close"].last().reindex(daily.index)
    morn_last = df[df["session"] == "morning"].groupby("date")["close"].last().reindex(daily.index)
    c_prev = daily["close"].shift(1)
    p1 = eve_last.shift(1).fillna(c_prev)
    p2 = morn_last.fillna(p1)
    daily["gap"] = np.log(daily["open"] / c_prev)
    daily["gap_evening"] = np.log(p1 / c_prev)
    daily["gap_morning"] = np.log(p2 / p1)
    daily["gap_open"] = np.log(daily["open"] / p2)
    return daily.loc[:, list(DAILY_COLUMNS)]


def add_har_features(daily: pd.DataFrame, windows: Sequence[int] = (1, 5, 22),
                     max_gap_days: int = 10) -> pd.DataFrame:
    """Добавляет rv_k и log_rv_k: среднее RV за k дней, включая день t.

    Неполные дни обнуляются; окна, пересекающие календарный разрыв > max_gap_days, дают NaN.
    """
    if not daily.index.is_monotonic_increasing:
        raise ValueError("Индекс должен быть отсортирован по возрастанию даты")
    cal_gap = daily.index.to_series().diff().dt.days
    segment = (cal_gap > max_gap_days).cumsum()
    pos = segment.groupby(segment).cumcount()
    rv_ok = daily["rv"].where(daily["is_complete"])
    out = daily.copy()
    out["days_since_break"] = pos
    for k in windows:
        mean_k = rv_ok.rolling(k, min_periods=k).mean().where(pos >= k - 1)
        out[f"rv_{k}"] = mean_k
        out[f"log_rv_{k}"] = np.log(mean_k.where(mean_k > 0))
    return out
    
def build_panel(secids: Sequence[str], start: date, end: date, *,
                config: FeatureConfig | None = None,
                data_dir: Path | str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """Панель признаков с индексом (date, secid); свечи берутся из кэша или докачиваются."""
    cfg = config or FeatureConfig()
    frames: list[pd.DataFrame] = []
    for secid in secids:
        candles = load_candles(secid, cfg.base_minutes, start, end, data_dir=data_dir)
        daily = add_har_features(compute_daily(candles, cfg), cfg.har_windows)
        daily.insert(0, "secid", secid)
        frames.append(daily.reset_index())
    return pd.concat(frames, ignore_index=True).set_index(["date", "secid"]).sort_index()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Панель RV/HAR в data/processed/panel.parquet")
    ap.add_argument("--secids", nargs="+", default=list(TICKERS))
    ap.add_argument("--start", type=date.fromisoformat, default=date(2022, 11, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--grid", type=int, default=10)
    ap.add_argument("--base", type=int, default=1)
    ap.add_argument("--alpha", type=float, default=0.10)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = FeatureConfig(grid_minutes=args.grid, base_minutes=args.base, alpha=args.alpha)
    panel = build_panel(args.secids, args.start, args.end, config=cfg, data_dir=args.data_dir)
    out = Path(args.data_dir) / "processed" / "panel.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    panel.reset_index().to_parquet(out, index=False, engine="pyarrow")
    summary = panel.groupby(level="secid").agg(
        n_days=("rv", "size"), n_incomplete=("is_complete", lambda s: int((~s).sum())),
        first=("open", lambda s: s.index.get_level_values("date").min()),
        last=("open", lambda s: s.index.get_level_values("date").max()),
        median_log_rv=("log_rv", "median"),
    )
    print(summary.to_string())
    print(f"Записано: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())