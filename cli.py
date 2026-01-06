import argparse
import os
from core.logger import Logger
from mt5_client import MT5Client
from config_loader import ConfigLoader
from strategy_manager import StrategyManager
from runner import Runner


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Grid trading runner (bounded loop; no infinite loops)")
    parser.add_argument(
        "--cycles",
        type=int,
        default=int(os.getenv("INV_CYCLES", "999999999")),
        help="How many cycles to run (default: 999999999).",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=float(os.getenv("INV_MAX_SECONDS", "0")),
        help="Optional max runtime in seconds; stops when exceeded (0 disables).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("INV_INTERVAL", "1.0")),
        help="Sleep interval between cycles in seconds (default: 1.0).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    mt5_client = MT5Client()
    if not mt5_client.initialize():
        Logger.log("SYSTEM", "ERROR", "系统初始化失败")
        return 1

    config_loader = ConfigLoader()
    strategy_manager = StrategyManager(mt5_client, config_loader)
    runner = Runner(mt5_client, strategy_manager)

    Logger.log(
        "SYSTEM",
        "START",
        f"系统已就绪 (cycles={args.cycles}, max_seconds={args.max_seconds}, interval={args.interval})",
    )

    try:
        runner.run(cycles=args.cycles, max_seconds=args.max_seconds, interval=args.interval)
    except KeyboardInterrupt:
        Logger.log("SYSTEM", "STOP", "手动停止")
    except Exception as exc:
        Logger.log("SYSTEM", "ERROR", f"运行异常: {exc}")
    finally:
        mt5_client.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
