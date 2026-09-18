from __future__ import annotations

from datetime import datetime, timedelta, timezone
import lzma
import struct
import unittest

from core.research.dukascopy import (
    DUKASCOPY_INSTRUMENTS,
    DukascopyBi5HistoryProvider,
    DukascopyDataError,
    HttpPayload,
)
from core.research.history import InstrumentSpec


UTC = timezone.utc
TICK = struct.Struct(">iiiff")


def payload(*records: tuple[int, int, int, float, float]) -> bytes:
    return lzma.compress(b"".join(TICK.pack(*record) for record in records))


class _SpecProvider:
    def __init__(self) -> None:
        self.calls = 0

    def specification(self, symbol: str) -> InstrumentSpec:
        self.calls += 1
        return InstrumentSpec(
            symbol=symbol,
            point=0.001,
            digits=3,
            contract_size=100.0,
            tick_size=0.001,
            tick_value=0.1,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )

    def bars(self, *args, **kwargs):
        raise AssertionError("Dukascopy 模式不得调用 MT5 历史 K 线")

    def ticks(self, *args, **kwargs):
        raise AssertionError("Dukascopy 模式不得调用 MT5 历史 tick")


class _Transport:
    def __init__(self, responses: list[HttpPayload]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, timeout_seconds: float) -> HttpPayload:
        self.urls.append(url)
        if not self.responses:
            return HttpPayload(404, b"")
        return self.responses.pop(0)


class DukascopyBi5Tests(unittest.TestCase):
    def test_url_uses_zero_based_month(self) -> None:
        url = DukascopyBi5HistoryProvider._hour_url("XAUUSD", datetime(2024, 1, 2, 3, tzinfo=UTC))
        self.assertIn("/XAUUSD/2024/00/02/03h_ticks.bi5", url)

    def test_decodes_big_endian_ticks_and_validates_bid_ask(self) -> None:
        hour = datetime(2024, 1, 8, tzinfo=UTC)
        values = DukascopyBi5HistoryProvider._decode_hour(
            payload((0, 2_000_001, 2_000_000, 1.0, 2.0), (60_000, 2_000_003, 2_000_002, 3.0, 4.0)),
            hour,
            DUKASCOPY_INSTRUMENTS["XAUUSD"],
        )
        self.assertEqual(values[0].timestamp, hour)
        self.assertEqual(values[0].bid, 2000.0)
        self.assertEqual(values[0].ask, 2000.001)
        self.assertEqual(values[1].volume, 7.0)

    def test_rejects_invalid_price_scale_or_crossed_quote(self) -> None:
        with self.assertRaises(DukascopyDataError):
            DukascopyBi5HistoryProvider._decode_hour(
                payload((0, 1_999_999, 2_000_000, 1.0, 1.0)),
                datetime(2024, 1, 8, tzinfo=UTC),
                DUKASCOPY_INSTRUMENTS["XAUUSD"],
            )

    def test_aggregates_tick_data_and_does_not_use_mt5_history(self) -> None:
        response = HttpPayload(
            200,
            payload(
                (0, 2_000_001, 2_000_000, 1.0, 1.0),
                (20_000, 2_000_004, 2_000_002, 2.0, 3.0),
                (70_000, 2_000_005, 2_000_003, 1.0, 1.0),
            ),
        )
        spec_provider = _SpecProvider()
        provider = DukascopyBi5HistoryProvider(
            spec_provider,
            source_symbol_map={"XAUUSDc": "XAUUSD"},
            transport=_Transport([response]),
            retry_backoff_seconds=0.0,
        )
        start = datetime(2024, 1, 8, tzinfo=UTC)
        bars = provider.bars("XAUUSDc", start, start + timedelta(minutes=1, seconds=59))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].open, 2000.0)
        self.assertEqual(bars[0].close, 2000.002)
        self.assertEqual(bars[0].spread, 2.0)
        self.assertGreater(spec_provider.calls, 0)

    def test_retries_transient_error_then_decodes(self) -> None:
        transport = _Transport(
            [
                HttpPayload(429, b""),
                HttpPayload(200, payload((0, 2_000_001, 2_000_000, 1.0, 1.0))),
            ]
        )
        provider = DukascopyBi5HistoryProvider(
            _SpecProvider(),
            source_symbol_map={"XAUUSDc": "XAUUSD"},
            transport=transport,
            retry_backoff_seconds=0.0,
        )
        values = provider.ticks("XAUUSDc", datetime(2024, 1, 8, tzinfo=UTC), datetime(2024, 1, 8, 0, 0, 1, tzinfo=UTC))
        self.assertEqual(len(values), 1)
        self.assertIn("重试=1", provider.describe_source("XAUUSDc"))

    def test_404_is_recorded_as_empty_hour(self) -> None:
        provider = DukascopyBi5HistoryProvider(
            _SpecProvider(),
            source_symbol_map={"XAUUSDc": "XAUUSD"},
            transport=_Transport([HttpPayload(404, b"")]),
            retry_backoff_seconds=0.0,
        )
        values = provider.ticks("XAUUSDc", datetime(2024, 1, 8, tzinfo=UTC), datetime(2024, 1, 8, 0, 0, 1, tzinfo=UTC))
        self.assertEqual(values, ())
        self.assertIn("空小时=1", provider.describe_source("XAUUSDc"))


if __name__ == "__main__":
    unittest.main()
