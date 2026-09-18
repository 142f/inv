"""MT5 历史行情端口、品种规格适配与数据质量校验。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, Sequence

from .models import Bar


UTC = timezone.utc


class HistoryUnavailableError(RuntimeError):
    """MT5 品种存在但终端未能提供所需历史行情。"""


@dataclass(frozen=True, slots=True)
class HistoricalTick:
    """回测逐笔行情的最小公共表示。"""

    timestamp: datetime
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """计算盈亏、仓位、隔夜费所需的完整品种规格。"""

    symbol: str
    point: float
    digits: int
    contract_size: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    freeze_level: int = 0
    swap_long: float | None = None
    swap_short: float | None = None
    swap_mode: str = "unknown"
    rollover3_weekday: int | None = None
    sessions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.point <= 0 or self.contract_size <= 0 or self.tick_size <= 0:
            raise ValueError("品种规格中的价格或合约大小无效")
        if self.volume_min <= 0 or self.volume_step <= 0 or self.volume_max < self.volume_min:
            raise ValueError("品种规格中的手数范围无效")

    def pnl(self, entry_price: float, exit_price: float, volume: float, side: str) -> float:
        """把价格变化换算为账户货币盈亏；不含佣金、滑点、隔夜费。"""

        direction = 1.0 if side == "buy" else -1.0
        delta = (exit_price - entry_price) * direction
        if self.tick_value > 0:
            return delta / self.tick_size * self.tick_value * volume
        return delta * self.contract_size * volume


class HistoryProvider(Protocol):
    """研究层的数据端口，策略与回测器均不依赖 MT5 模块。"""

    def bars(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "M1"
    ) -> Sequence[Bar]: ...

    def ticks(self, symbol: str, start: datetime, end: datetime) -> Sequence[HistoricalTick]: ...

    def specification(self, symbol: str) -> InstrumentSpec: ...


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """历史行情校验结论；问题不会被静默忽略。"""

    symbol: str
    total_bars: int
    valid_bars: int
    duplicate_bars: int
    invalid_bars: int
    gap_count: int
    largest_gap_seconds: float
    coverage_ratio: float
    warnings: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        return self.valid_bars > 0 and self.invalid_bars == 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("历史数据时间必须带时区")
    return value.astimezone(UTC)


def _bar_time(value: datetime | float) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    return datetime.fromtimestamp(float(value), tz=UTC)


def _is_weekend_gap(previous: datetime, current: datetime) -> bool:
    """忽略常见周末休市的 XAU 间隔，仍保留 BTC 的所有间隔。"""

    cursor = previous
    while cursor < current:
        if cursor.weekday() >= 5:
            return True
        cursor += timedelta(hours=1)
    return False


def validate_m1_bars(
    symbol: str,
    bars: Sequence[Bar],
    start: datetime,
    end: datetime,
    *,
    trades_24_7: bool,
) -> DataQualityReport:
    """检查排序、重复、OHLC 合法性与关键分钟缺口。"""

    start, end = _utc(start), _utc(end)
    ordered = sorted(bars, key=lambda item: _bar_time(item.timestamp))
    valid: list[Bar] = []
    seen: set[datetime] = set()
    duplicate = invalid = gaps = 0
    largest_gap = 0.0
    warnings: list[str] = []

    for bar in ordered:
        timestamp = _bar_time(bar.timestamp)
        if timestamp in seen:
            duplicate += 1
            continue
        seen.add(timestamp)
        if not (start <= timestamp <= end) or min(bar.open, bar.high, bar.low, bar.close) <= 0:
            invalid += 1
            continue
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            invalid += 1
            continue
        valid.append(bar)

    for previous, current in zip(valid, valid[1:]):
        previous_time, current_time = _bar_time(previous.timestamp), _bar_time(current.timestamp)
        seconds = (current_time - previous_time).total_seconds()
        if seconds > 180 and (trades_24_7 or not _is_weekend_gap(previous_time, current_time)):
            gaps += 1
            largest_gap = max(largest_gap, seconds)

    total_minutes = max(1, int((end - start).total_seconds() // 60) + 1)
    expected_minutes = total_minutes if trades_24_7 else max(1, int(total_minutes * 5 / 7))
    coverage = min(1.0, len(valid) / expected_minutes)
    if duplicate:
        warnings.append(f"发现 {duplicate} 根重复 M1 K 线，已在校验中去重")
    if invalid:
        warnings.append(f"发现 {invalid} 根异常 M1 K 线")
    if gaps:
        warnings.append(f"发现 {gaps} 个非休市缺口，最大 {largest_gap / 60:.1f} 分钟")
    return DataQualityReport(
        symbol=symbol,
        total_bars=len(bars),
        valid_bars=len(valid),
        duplicate_bars=duplicate,
        invalid_bars=invalid,
        gap_count=gaps,
        largest_gap_seconds=largest_gap,
        coverage_ratio=coverage,
        warnings=tuple(warnings),
    )


class Mt5HistoryProvider:
    """将 MT5 网关返回的原始历史记录转换为研究层对象。

    该适配器只调用显式区间查询接口，不读取任何工作区数据，也不下发交易指令。
    """

    _SWAP_MODES = {
        0: "disabled",
        1: "points",
        2: "currency_symbol",
        3: "currency_margin",
        4: "currency_deposit",
        5: "interest_current",
        6: "interest_open",
        7: "reopen_current",
        8: "reopen_bid",
    }

    def __init__(self, broker: Any, *, rate_chunk_days: int = 30) -> None:
        if rate_chunk_days < 1:
            raise ValueError("历史 K 线分段天数必须为正数")
        self._broker = broker
        self._rate_chunk_days = rate_chunk_days

    def _rates_chunk(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Any:
        """优先区间读取；终端不支持该调用时退回到起始时间读取。"""

        records = self._broker.copy_rates_range(symbol, timeframe, start, end)
        if records is not None:
            return records
        fallback = getattr(self._broker, "copy_rates_from", None)
        if fallback is None:
            return None
        minutes = max(1, int((end - start).total_seconds() // 60) + 1)
        return fallback(symbol, timeframe, start, minutes)

    @staticmethod
    def _record_value(record: Any, name: str, default: Any = 0) -> Any:
        if isinstance(record, dict):
            return record.get(name, default)
        try:
            return record[name]
        except (TypeError, IndexError, KeyError):
            return getattr(record, name, default)

    def bars(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "M1"
    ) -> Sequence[Bar]:
        start, end = _utc(start), _utc(end)
        if start > end:
            raise ValueError("历史 K 线起始时间不能晚于结束时间")
        # MT5 的“最大图表 K 线”会限制一次性请求；分段查询避免长区间被静默截断。
        records: list[Any] = []
        failed_chunks = 0
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(days=self._rate_chunk_days))
            chunk = self._rates_chunk(symbol, timeframe, cursor, chunk_end)
            if chunk is None:
                failed_chunks += 1
            else:
                records.extend(chunk)
            cursor = chunk_end
        if not records and failed_chunks:
            last_error = getattr(self._broker, "last_error", lambda: None)()
            raise HistoryUnavailableError(
                f"MT5 未返回 {symbol} 在 {start.isoformat()} 至 {end.isoformat()} 的 {timeframe} 历史；"
                f"失败分段 {failed_chunks}，终端错误={last_error}"
            )
        # 分段端点含有同一根 K 线，按开盘时间去重后再转换领域对象。
        deduplicated: dict[float, Any] = {}
        for item in records:
            timestamp = float(self._record_value(item, "time"))
            deduplicated.setdefault(timestamp, item)
        return tuple(
            Bar(
                timestamp=datetime.fromtimestamp(float(self._record_value(item, "time")), tz=UTC),
                open=float(self._record_value(item, "open")),
                high=float(self._record_value(item, "high")),
                low=float(self._record_value(item, "low")),
                close=float(self._record_value(item, "close")),
                volume=float(self._record_value(item, "tick_volume", 0.0)),
                spread=float(self._record_value(item, "spread", 0.0)),
            )
            for _, item in sorted(deduplicated.items())
        )

    def ticks(self, symbol: str, start: datetime, end: datetime) -> Sequence[HistoricalTick]:
        records = self._broker.copy_ticks_range(symbol, _utc(start), _utc(end)) or ()
        return tuple(
            HistoricalTick(
                timestamp=datetime.fromtimestamp(
                    float(self._record_value(item, "time_msc", 0)) / 1000.0
                    if self._record_value(item, "time_msc", 0)
                    else float(self._record_value(item, "time")),
                    tz=UTC,
                ),
                bid=float(self._record_value(item, "bid", 0.0)),
                ask=float(self._record_value(item, "ask", 0.0)),
                last=float(self._record_value(item, "last", 0.0)),
                volume=float(self._record_value(item, "volume", 0.0)),
            )
            for item in records
        )

    def specification(self, symbol: str) -> InstrumentSpec:
        info = self._broker.symbol_info(symbol)
        if info is None:
            raise ValueError(f"MT5 未返回 {symbol} 的品种规格")
        getter = lambda name, default=0: getattr(info, name, default)
        raw_swap_mode = getter("swap_mode", -1)
        return InstrumentSpec(
            symbol=symbol,
            point=float(getter("point")),
            digits=int(getter("digits")),
            contract_size=float(getter("trade_contract_size")),
            tick_size=float(getter("trade_tick_size", getter("point"))),
            tick_value=float(getter("trade_tick_value", 0.0)),
            volume_min=float(getter("volume_min")),
            volume_max=float(getter("volume_max")),
            volume_step=float(getter("volume_step")),
            stops_level=int(getter("trade_stops_level", 0)),
            freeze_level=int(getter("trade_freeze_level", 0)),
            swap_long=float(getter("swap_long")) if getter("swap_long", None) is not None else None,
            swap_short=float(getter("swap_short")) if getter("swap_short", None) is not None else None,
            swap_mode=self._SWAP_MODES.get(raw_swap_mode, str(raw_swap_mode)),
            rollover3_weekday=int(getter("swap_rollover3days", -1))
            if getter("swap_rollover3days", None) is not None
            else None,
        )
