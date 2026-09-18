"""Dukascopy 公开 BI5 历史文件适配器。

该模块仅用于研究回测：它不会保存原始行情文件，也不会把公开报价标记为经纪商成交
数据。公开 BI5 URL 并非 Dukascopy 受审核 HTTP API 的替代品，调用方必须在报告中
披露这一数据来源限制。
"""

from __future__ import annotations

import lzma
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Callable, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .history import HistoricalTick, HistoryProvider, InstrumentSpec, validate_m1_bars
from .models import Bar


UTC = timezone.utc
_TICK_RECORD = struct.Struct(">iiiff")
_HOUR_MILLISECONDS = 60 * 60 * 1000


class DukascopyDataError(RuntimeError):
    """公开 BI5 数据无法安全用于回测时抛出。"""


@dataclass(frozen=True, slots=True)
class DukascopyInstrument:
    """公开 BI5 二进制报价的解析元数据。"""

    source_symbol: str
    price_divisor: float
    minimum_price: float
    maximum_price: float


DUKASCOPY_INSTRUMENTS: Mapping[str, DukascopyInstrument] = {
    "XAUUSD": DukascopyInstrument("XAUUSD", 1_000.0, 500.0, 10_000.0),
    "BTCUSD": DukascopyInstrument("BTCUSD", 100.0, 1_000.0, 1_000_000.0),
}


@dataclass(frozen=True, slots=True)
class HttpPayload:
    """最小 HTTP 传输结果，便于无网络单元测试。"""

    status: int
    body: bytes


class Bi5Transport(Protocol):
    def get(self, url: str, timeout_seconds: float) -> HttpPayload: ...


class UrlLibBi5Transport:
    """使用标准库发起只读 HTTP 请求，不引入额外第三方依赖。"""

    def get(self, url: str, timeout_seconds: float) -> HttpPayload:
        request = Request(url, headers={"User-Agent": "inv-research-bi5/1.0"})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - 固定的公开数据地址
                return HttpPayload(int(response.status), response.read())
        except HTTPError as error:
            return HttpPayload(int(error.code), error.read())
        except (URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", str(error))
            raise DukascopyDataError(f"BI5 网络请求失败：{reason}") from error


@dataclass(slots=True)
class _DownloadStats:
    requested_hours: int = 0
    empty_hours: int = 0
    retries: int = 0
    decoded_ticks: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Dukascopy 时间必须带 UTC 时区")
    return value.astimezone(UTC)


def _hour_range(start: datetime, end: datetime) -> tuple[datetime, ...]:
    cursor = _utc(start).replace(minute=0, second=0, microsecond=0)
    end = _utc(end)
    result: list[datetime] = []
    while cursor <= end:
        result.append(cursor)
        cursor += timedelta(hours=1)
    return tuple(result)


class DukascopyBi5HistoryProvider:
    """从公开 BI5 tick 文件聚合 M1，并按需返回逐笔报价。

    ``specification_provider`` 只负责提供当前 MT5 的合约与 swap 规格；本类绝不会调用
    其历史行情方法。因此公开行情和经纪商规格的边界保持可审计。
    """

    base_url = "https://datafeed.dukascopy.com/datafeed"

    def __init__(
        self,
        specification_provider: HistoryProvider,
        *,
        source_symbol_map: Mapping[str, str],
        transport: Bi5Transport | None = None,
        max_workers: int = 4,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        if max_workers < 1 or max_workers > 4:
            raise ValueError("BI5 并发数必须在 1 到 4 之间")
        if timeout_seconds <= 0 or max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("BI5 网络参数无效")
        self._specification_provider = specification_provider
        self._source_symbol_map = {str(key).upper(): str(value).upper() for key, value in source_symbol_map.items()}
        self._transport = transport or UrlLibBi5Transport()
        self._max_workers = max_workers
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._stats: dict[str, _DownloadStats] = {}
        self._stats_lock = Lock()

    def specification(self, symbol: str) -> InstrumentSpec:
        return self._specification_provider.specification(symbol)

    def bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "M1") -> Sequence[Bar]:
        if timeframe.upper() != "M1":
            raise ValueError("公开 BI5 回测提供器当前只支持 M1 聚合")
        source = self._source_symbol(symbol)
        spec = self.specification(symbol)
        ticks_by_hour = self._download_hours(symbol, source, start, end)
        result: list[Bar] = []
        for ticks in ticks_by_hour:
            result.extend(self._aggregate_m1(ticks, spec))
        start, end = _utc(start), _utc(end)
        deduplicated = {item.timestamp: item for item in result if start <= item.timestamp <= end}
        return tuple(deduplicated[key] for key in sorted(deduplicated))

    def ticks(self, symbol: str, start: datetime, end: datetime) -> Sequence[HistoricalTick]:
        source = self._source_symbol(symbol)
        start, end = _utc(start), _utc(end)
        result: list[HistoricalTick] = []
        for values in self._download_hours(symbol, source, start, end):
            result.extend(item for item in values if start <= item.timestamp <= end)
        deduplicated = {(item.timestamp, item.bid, item.ask, item.volume): item for item in result}
        return tuple(deduplicated[key] for key in sorted(deduplicated))

    def smoke_validate(self, symbol: str, start: datetime, *, trades_24_7: bool, days: int = 7) -> None:
        """下载连续七天并验证二进制、报价和 M1 聚合，失败则阻止全量请求。"""

        if days < 1:
            raise ValueError("验收天数必须为正数")
        start = _utc(start)
        end = start + timedelta(days=days) - timedelta(seconds=1)
        values = self.bars(symbol, start, end)
        quality = validate_m1_bars(symbol, values, start, end, trades_24_7=trades_24_7)
        expected_minimum = 8_000 if trades_24_7 else 5_000
        if not quality.is_usable or quality.valid_bars < expected_minimum:
            raise DukascopyDataError(
                f"{symbol} 的 {days} 天 BI5 验收未通过：有效 M1={quality.valid_bars}，"
                f"最低要求={expected_minimum}，异常={quality.invalid_bars}，缺口={quality.gap_count}"
            )

    def describe_source(self, symbol: str) -> str:
        stats = self._stats.get(symbol.upper(), _DownloadStats())
        source = self._source_symbol(symbol)
        return (
            f"Dukascopy 公开 BI5 tick 聚合行情（非官方 HTTP API 实现，源品种={source}，"
            f"请求小时={stats.requested_hours}，空小时={stats.empty_hours}，重试={stats.retries}）"
        )

    def _source_symbol(self, symbol: str) -> str:
        source = self._source_symbol_map.get(symbol.upper())
        if source is None:
            raise DukascopyDataError(f"未配置 {symbol} 对应的 Dukascopy 源品种")
        if source not in DUKASCOPY_INSTRUMENTS:
            raise DukascopyDataError(f"公开 BI5 解析器不支持源品种 {source}")
        return source

    def _download_hours(
        self, report_symbol: str, source: str, start: datetime, end: datetime
    ) -> tuple[tuple[HistoricalTick, ...], ...]:
        hours = _hour_range(start, end)
        # 每个日期单独调度，避免全区间任务一次性驻留内存。
        grouped: dict[datetime.date, list[datetime]] = {}
        for hour in hours:
            grouped.setdefault(hour.date(), []).append(hour)
        result: list[tuple[HistoricalTick, ...]] = []
        for group in grouped.values():
            executor = ThreadPoolExecutor(max_workers=self._max_workers)
            future_map = {executor.submit(self._download_hour, report_symbol, source, hour): hour for hour in group}
            completed: dict[datetime, tuple[HistoricalTick, ...]] = {}
            try:
                for future in as_completed(future_map):
                    completed[future_map[future]] = future.result()
            except Exception:
                for pending in future_map:
                    pending.cancel()
                # 失败时不等待同批剩余排队任务，避免网络不可达导致 smoke 长时间挂起。
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
            result.extend(completed[hour] for hour in sorted(completed))
        return tuple(result)

    def _download_hour(self, report_symbol: str, source: str, hour: datetime) -> tuple[HistoricalTick, ...]:
        metadata = DUKASCOPY_INSTRUMENTS[source]
        url = self._hour_url(source, hour)
        self._increment(report_symbol, "requested_hours")
        for attempt in range(self._max_retries + 1):
            try:
                response = self._transport.get(url, self._timeout_seconds)
            except DukascopyDataError:
                if attempt >= self._max_retries:
                    raise
                self._retry(report_symbol, attempt)
                continue
            if response.status == 404:
                self._increment(report_symbol, "empty_hours")
                return ()
            if response.status in {429, 500, 502, 503, 504}:
                if attempt >= self._max_retries:
                    raise DukascopyDataError(f"BI5 请求反复失败：HTTP {response.status}，时间={hour.isoformat()}")
                self._retry(report_symbol, attempt)
                continue
            if response.status != 200:
                raise DukascopyDataError(f"BI5 请求失败：HTTP {response.status}，时间={hour.isoformat()}")
            ticks = self._decode_hour(response.body, hour, metadata)
            self._increment(report_symbol, "decoded_ticks", len(ticks))
            return ticks
        raise AssertionError("不可达")

    def _retry(self, symbol: str, attempt: int) -> None:
        self._increment(symbol, "retries")
        time.sleep(self._retry_backoff_seconds * (2**attempt))

    def _increment(self, symbol: str, field_name: str, value: int = 1) -> None:
        with self._stats_lock:
            stats = self._stats.setdefault(symbol.upper(), _DownloadStats())
            setattr(stats, field_name, getattr(stats, field_name) + value)

    @classmethod
    def _hour_url(cls, symbol: str, hour: datetime) -> str:
        hour = _utc(hour)
        # Dukascopy 的月份采用 0 基索引：一月为 00。
        return (
            f"{cls.base_url}/{symbol}/{hour.year}/{hour.month - 1:02d}/"
            f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
        )

    @staticmethod
    def _decode_hour(payload: bytes, hour: datetime, metadata: DukascopyInstrument) -> tuple[HistoricalTick, ...]:
        if not payload:
            return ()
        try:
            decoded = lzma.decompress(payload)
        except lzma.LZMAError as exc:
            raise DukascopyDataError(f"BI5 LZMA 解压失败，时间={_utc(hour).isoformat()}") from exc
        if len(decoded) % _TICK_RECORD.size:
            raise DukascopyDataError(f"BI5 记录长度无效，时间={_utc(hour).isoformat()}")
        result: list[HistoricalTick] = []
        previous_offset = -1
        hour = _utc(hour)
        for offset, raw_ask, raw_bid, ask_volume, bid_volume in _TICK_RECORD.iter_unpack(decoded):
            if not 0 <= offset < _HOUR_MILLISECONDS or offset < previous_offset:
                raise DukascopyDataError(f"BI5 tick 时间偏移无效，时间={hour.isoformat()}")
            previous_offset = offset
            ask, bid = raw_ask / metadata.price_divisor, raw_bid / metadata.price_divisor
            if not (metadata.minimum_price <= bid <= ask <= metadata.maximum_price):
                raise DukascopyDataError(
                    f"BI5 报价或价格缩放无效，品种={metadata.source_symbol}，时间={hour.isoformat()}"
                )
            result.append(
                HistoricalTick(
                    timestamp=hour + timedelta(milliseconds=offset),
                    bid=bid,
                    ask=ask,
                    last=0.0,
                    volume=max(0.0, float(ask_volume)) + max(0.0, float(bid_volume)),
                )
            )
        return tuple(result)

    @staticmethod
    def _aggregate_m1(ticks: Sequence[HistoricalTick], spec: InstrumentSpec) -> tuple[Bar, ...]:
        groups: dict[datetime, list[HistoricalTick]] = {}
        for tick in ticks:
            minute = tick.timestamp.replace(second=0, microsecond=0)
            groups.setdefault(minute, []).append(tick)
        result: list[Bar] = []
        for minute, values in sorted(groups.items()):
            ordered = sorted(values, key=lambda item: item.timestamp)
            bids = [item.bid for item in ordered]
            # 网格回测器沿用 MT5 的“点数 spread”语义，边界处转换为价格。
            spread_points = round((ordered[-1].ask - ordered[-1].bid) / spec.point, 8)
            result.append(
                Bar(
                    timestamp=minute,
                    open=bids[0],
                    high=max(bids),
                    low=min(bids),
                    close=bids[-1],
                    spread=max(0.0, spread_points),
                    volume=sum(item.volume for item in ordered),
                )
            )
        return tuple(result)
