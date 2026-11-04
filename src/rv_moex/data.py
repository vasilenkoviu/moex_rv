"""Загрузка свечей MOEX ISS: пагинация, повторные попытки, кэш parquet, контроль качества."""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://iss.moex.com/iss"
BOARD_PATH = "engines/stock/markets/shares/boards/TQBR"
VALID_INTERVALS = frozenset({1, 10, 60, 24})
TICKERS: tuple[str, ...] = (
    "SBER", "GAZP", "LKOH", "VTBR", "T", "ROSN", "NVTK", "OZON", "PLZL", "GMKN",
)
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
PRICE_COLUMNS = ("open", "high", "low", "close")
CANDLE_COLUMNS = ("begin", "end", "open", "high", "low", "close", "value", "volume")
MAIN_START_MIN = 590
MAIN_END_MIN = 1130
EVENING_START_MIN = 1140
BACKOFF = 1.0
RETRIES = 5


class IssError(RuntimeError):
    """Ошибка обращения к MOEX ISS."""


class _Retryable(IssError):
    """Временная ошибка, допускающая повтор."""


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "rv-moex/0.1"
    return session


def _check_interval(interval: int) -> None:
    if interval not in VALID_INTERVALS:
        raise ValueError(
            f"Интервал {interval} не поддерживается; допустимы {sorted(VALID_INTERVALS)}. "
            "Интервал 5 мин ISS молча возвращает пустым; стройте его агрегацией минутных свечей."
        )


def _get_json(session: requests.Session, url: str, params: dict[str, Any],
              timeout: float = 30.0) -> dict[str, Any]:
    """GET с повторными попытками; 4xx (кроме 429) не повторяются."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _Retryable(f"HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise IssError(f"HTTP {resp.status_code}: {resp.url}")
            return resp.json()
        except (requests.RequestException, ValueError, _Retryable) as exc:
            last = exc
            delay = BACKOFF * 2**attempt + random.uniform(0.0, 0.5)
            log.warning("Попытка %d/%d не удалась (%s); пауза %.1f с", attempt + 1, RETRIES, exc, delay)
            time.sleep(delay)
    raise IssError(f"Исчерпаны попытки: {url}") from last


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({
        c: pd.Series(dtype="datetime64[ns]" if c in ("begin", "end") else "float64")
        for c in CANDLE_COLUMNS
    })


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.loc[:, list(CANDLE_COLUMNS)].copy()
    for c in ("begin", "end"):
        df[c] = pd.to_datetime(df[c]).astype("datetime64[ns]")
    for c in CANDLE_COLUMNS[2:]:
        df[c] = df[c].astype("float64")
    return df


def _date_chunks(first: date, last: date, step: int) -> Iterator[tuple[date, date]]:
    cur = first
    while cur <= last:
        stop = min(cur + timedelta(days=step - 1), last)
        yield cur, stop
        cur = stop + timedelta(days=1)


def _fetch_chunk(session: requests.Session, secid: str, interval: int,
                 first: date, last: date, pause: float) -> pd.DataFrame:
    url = f"{BASE_URL}/{BOARD_PATH}/securities/{secid}/candles.json"
    frames: list[pd.DataFrame] = []
    offset = 0
    while True:
        body = _get_json(session, url, {
            "iss.meta": "off", "interval": interval, "start": offset,
            "from": first.isoformat(), "till": f"{last.isoformat()} 23:59:59",
        })
        block = body.get("candles")
        if block is None:
            raise IssError(f"В ответе нет блока candles: {secid}")
        rows = block["data"]
        if not rows:
            break
        frames.append(pd.DataFrame(rows, columns=block["columns"]))
        offset += len(rows)
        time.sleep(pause)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_candles(secid: str, interval: int, start: date, end: date, *,
                  session: requests.Session, pause: float = 0.05) -> pd.DataFrame:
    """Загружает свечи [start, end] с пагинацией; без очистки и без кэша."""
    _check_interval(interval)
    step = 31 if interval < 24 else 366
    frames = [_fetch_chunk(session, secid, interval, a, b, pause)
              for a, b in _date_chunks(start, end, step)]
    frames = [f for f in frames if not f.empty]
    return _normalize(pd.concat(frames, ignore_index=True)) if frames else _empty_frame()


def clean_candles(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Удаляет дубли по begin (оставляет последнюю запись), сортирует; возвращает число дублей."""
    n_dup = int(df.duplicated("begin").sum())
    out = df.drop_duplicates("begin", keep="last").sort_values("begin").reset_index(drop=True)
    return out, n_dup


def session_label(begin: pd.Series) -> pd.Series:
    """Метка сессии по времени начала свечи: morning / main / evening / other."""
    m = begin.dt.hour * 60 + begin.dt.minute
    conds = [m < MAIN_START_MIN, (m >= MAIN_START_MIN) & (m < MAIN_END_MIN), m >= EVENING_START_MIN]
    return pd.Series(np.select(conds, ["morning", "main", "evening"], default="other"), index=begin.index)


def cache_paths(secid: str, interval: int, data_dir: Path | str) -> tuple[Path, Path]:
    base = Path(data_dir) / "raw"
    stem = f"{secid}_{interval}"
    return base / f"{stem}.parquet", base / f"{stem}.json"


def _read_meta(path: Path) -> tuple[date, date] | None:
    if not path.exists():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    return date.fromisoformat(meta["covered_from"]), date.fromisoformat(meta["covered_to"])


def _write_cache(df: pd.DataFrame, pq: Path, meta: Path, cov: tuple[date, date]) -> None:
    pq.parent.mkdir(parents=True, exist_ok=True)
    tmp = pq.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, engine="pyarrow")
    os.replace(tmp, pq)
    tmp_meta = meta.with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps({"covered_from": cov[0].isoformat(), "covered_to": cov[1].isoformat()}),
                        encoding="utf-8")
    os.replace(tmp_meta, meta)


def load_candles(secid: str, interval: int, start: date, end: date, *,
                 data_dir: Path | str = DEFAULT_DATA_DIR, refresh: bool = False,
                 session: requests.Session | None = None, pause: float = 0.05) -> pd.DataFrame:
    """Возвращает свечи [start, end] из кэша, докачивая недостающие диапазоны.

    Покрытие кэша хранится в json; сегодняшний (неполный) день всегда перезагружается.
    """
    _check_interval(interval)
    if start > end:
        raise ValueError("start позже end")
    pq, meta = cache_paths(secid, interval, data_dir)
    cov = None if refresh or not pq.exists() else _read_meta(meta)
    cached = pd.read_parquet(pq) if cov is not None else _empty_frame()
    yesterday = date.today() - timedelta(days=1)

    if cov is None:
        ranges = [(start, end)]
        new_cov = (start, min(end, yesterday))
    else:
        ranges = []
        if start < cov[0]:
            ranges.append((start, cov[0] - timedelta(days=1)))
        if end > cov[1]:
            ranges.append((cov[1] + timedelta(days=1), end))
        new_cov = (min(cov[0], start), max(cov[1], min(end, yesterday)))

    fresh: list[pd.DataFrame] = []
    if ranges:
        sess = session if session is not None else _new_session()
        try:
            fresh = [fetch_candles(secid, interval, a, b, session=sess, pause=pause) for a, b in ranges]
        finally:
            if session is None:
                sess.close()
        n_dup = sum(clean_candles(f)[1] for f in fresh)
        if n_dup:
            log.warning("%s: при загрузке найдено дублей: %d", secid, n_dup)

    parts = [f for f in [cached, *fresh] if not f.empty]
    merged = clean_candles(pd.concat(parts, ignore_index=True))[0] if parts else _empty_frame()
    if ranges or cov is None:
        _write_cache(merged, pq, meta, new_cov)

    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    return merged[(merged["begin"] >= lo) & (merged["begin"] < hi)].reset_index(drop=True)


@dataclass(frozen=True)
class QualityReport:
    """Сводка качества: n_missing_slots — пропущенные узлы сетки внутри одной сессии одного дня."""

    secid: str
    interval: int
    n_rows: int
    first: str | None
    last: str | None
    n_days: int
    n_weekend_days: int
    n_duplicates: int
    n_bad_prices: int
    n_missing_slots: int


def check_candles(df: pd.DataFrame, secid: str, interval: int) -> QualityReport:
    """Проверяет дубли, некорректные цены и пропуски сетки (для внутридневных интервалов)."""
    if df.empty:
        return QualityReport(secid, interval, 0, None, None, 0, 0, 0, 0, 0)
    px = df.loc[:, list(PRICE_COLUMNS)]
    body_hi, body_lo = df[["open", "close"]].max(axis=1), df[["open", "close"]].min(axis=1)
    bad = (px.isna().any(axis=1) | (px <= 0).any(axis=1) | (df["high"] < df["low"])
           | (df["high"] < body_hi) | (df["low"] > body_lo))
    day = df["begin"].dt.normalize()
    n_missing = 0
    if interval < 24:
        seg = session_label(df["begin"])
        gap = df["begin"].groupby([day, seg], sort=False).diff().dt.total_seconds() / (60 * interval) - 1
        n_missing = int(gap.round().clip(lower=0).fillna(0).sum())
    return QualityReport(
        secid=secid, interval=interval, n_rows=len(df),
        first=str(df["begin"].min()), last=str(df["begin"].max()),
        n_days=int(day.nunique()),
        n_weekend_days=int(day[df["begin"].dt.day_of_week >= 5].nunique()),
        n_duplicates=int(df.duplicated("begin").sum()),
        n_bad_prices=int(bad.sum()), n_missing_slots=n_missing,
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Загрузка свечей MOEX ISS в data/raw")
    ap.add_argument("--secids", nargs="+", default=list(TICKERS))
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2022, 11, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    reports: list[QualityReport] = []
    with _new_session() as session:
        for secid in args.secids:
            df = load_candles(secid, args.interval, args.start, args.end, data_dir=args.data_dir,
                              refresh=args.refresh, session=session)
            reports.append(check_candles(df, secid, args.interval))
    print(pd.DataFrame([asdict(r) for r in reports]).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())