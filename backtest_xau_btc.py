"""执行 XAUUSD/BTCUSD 标准化只读历史回测。

示例：
    python backtest_xau_btc.py --xau-suffix .a --btc-suffix .a --output-dir reports/backtests

该入口不读取策略配置、不创建交易指令，也不会调用 ``order_send``。它只连接已登录的
MT5 终端以取得公开的历史行情和品种规格。
"""

from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5

from core.research import (
    BacktestProfile,
    DukascopyBi5HistoryProvider,
    Mt5HistoryProvider,
    StandardComparisonRunner,
    resolve_broker_symbol,
)


class _ReadOnlyMt5Gateway:
    """只暴露历史回测需要的三个 MT5 读取接口。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def symbol_info(self, symbol: str) -> Any:
        with self._lock:
            return mt5.symbol_info(symbol)

    def copy_rates_range(self, symbol: str, timeframe: str, start, end) -> Any:
        native = getattr(mt5, f"TIMEFRAME_{timeframe.upper()}")
        with self._lock:
            return mt5.copy_rates_range(symbol, native, start, end)

    def copy_rates_from(self, symbol: str, timeframe: str, start, count: int) -> Any:
        native = getattr(mt5, f"TIMEFRAME_{timeframe.upper()}")
        with self._lock:
            return mt5.copy_rates_from(symbol, native, start, count)

    def copy_ticks_range(self, symbol: str, start, end) -> Any:
        with self._lock:
            return mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)

    def last_error(self) -> Any:
        return mt5.last_error()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XAUUSD 与 BTCUSD 标准化只读历史回测")
    parser.add_argument("--xau-suffix", default="", help="XAUUSD 的经纪商后缀，例如 .a")
    parser.add_argument("--btc-suffix", default="", help="BTCUSD 的经纪商后缀，例如 .a")
    parser.add_argument("--xau-symbol", default="", help="指定 XAU 实际品种名；为空时自动从 MT5 目录解析")
    parser.add_argument("--btc-symbol", default="", help="指定 BTC 实际品种名；为空时自动从 MT5 目录解析")
    parser.add_argument("--data-source", choices=("mt5", "dukascopy-bi5"), default="mt5", help="历史行情来源")
    parser.add_argument("--smoke-days", type=int, default=7, help="Dukascopy 全量前的连续验收天数")
    parser.add_argument("--smoke-only", action="store_true", help="仅执行 Dukascopy BI5 验收，不启动完整回测")
    parser.add_argument("--output-dir", type=Path, default=Path("reports") / "backtests", help="报告输出目录")
    parser.add_argument("--skip-tick-review", action="store_true", help="仅运行 M1 保守路径，不请求逐笔覆盖复核")
    return parser.parse_args()


def _resolve_and_select(canonical: str, preferred: str) -> str | None:
    records = mt5.symbols_get() or ()
    actual = resolve_broker_symbol(canonical, (item.name for item in records), preferred=preferred)
    if actual is None:
        return None
    return actual if mt5.symbol_select(actual, True) else None


def main() -> int:
    args = _parse_args()
    if not mt5.initialize():
        print(f"MT5 初始化失败：{mt5.last_error()}", file=sys.stderr)
        return 2
    try:
        xau_requested = args.xau_symbol or f"XAUUSD{args.xau_suffix}"
        btc_requested = args.btc_symbol or f"BTCUSD{args.btc_suffix}"
        xau_symbol = _resolve_and_select("XAUUSD", xau_requested)
        btc_symbol = _resolve_and_select("BTCUSD", btc_requested)
        if xau_symbol is None or btc_symbol is None:
            print(
                f"无法自动解析或选择品种：XAU={xau_requested}，BTC={btc_requested}，错误={mt5.last_error()}",
                file=sys.stderr,
            )
            return 2
        print(f"已解析 MT5 品种：XAU={xau_symbol}；BTC={btc_symbol}")
        specification_provider = Mt5HistoryProvider(_ReadOnlyMt5Gateway())
        profile = replace(BacktestProfile(), xau_symbol=xau_symbol, btc_symbol=btc_symbol)
        if args.data_source == "dukascopy-bi5":
            if args.smoke_days < 1:
                print("验收天数必须为正数", file=sys.stderr)
                return 2
            provider = DukascopyBi5HistoryProvider(
                specification_provider,
                source_symbol_map={xau_symbol: "XAUUSD", btc_symbol: "BTCUSD"},
            )
            # 避开元旦休市周，先在两个正常交易周执行二进制与 M1 聚合验收。
            smoke_start = profile.start + timedelta(days=7)
            provider.smoke_validate(xau_symbol, smoke_start, trades_24_7=False, days=args.smoke_days)
            provider.smoke_validate(btc_symbol, smoke_start, trades_24_7=True, days=args.smoke_days)
            print(f"Dukascopy BI5 {args.smoke_days} 天验收通过，开始完整区间回测")
            if args.smoke_only:
                return 0
        else:
            provider = specification_provider
        report = StandardComparisonRunner(provider, profile).run(request_tick_review=not args.skip_tick_review)
        json_path, markdown_path = report.write(args.output_dir)
        print(f"回测完成：{json_path}")
        print(f"回测摘要：{markdown_path}")
        return 0
    except Exception as exc:
        print(f"回测未生成正式结论：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
