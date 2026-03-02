"""量化系统主执行入口：环境初始化、参数解析与守护进程启动。"""

import argparse
import os
import sys
import traceback

from core.logger import Logger
from core.broker import MT5Broker
from core.config import ConfigLoader
from core.strategy.manager import StrategyManager
from core.runtime import Runner


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="网格交易执行器 (有界循环，无死循环)")
    
    # 【修改点】使用更稳健的短路计算逻辑 (or)，防止空字符串 ("") 引发 ValueError 崩溃
    parser.add_argument(
        "--cycles",
        type=int,
        default=int(os.environ.get("INV_CYCLES") or 999999999),
        help="最大循环执行次数 (默认: 999999999)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=float(os.environ.get("INV_MAX_SECONDS") or 0.0),
        help="最大运行时间限制(秒)，超时自动退出，0 表示不限制",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("INV_INTERVAL") or 1.0),
        help="每次循环间的休眠间隔(秒) (默认: 1.0)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    broker = MT5Broker()
    if not broker.initialize():
        Logger.log("系统", "致命错误", "MT5 终端初始化失败，请检查终端连接状态或授权")
        return 1

    # 【修改点】将组件初始化也纳入 try-except 保护伞，防止配置文件格式错误导致进程裸奔退出
    try:
        config_loader = ConfigLoader()
        strategy_manager = StrategyManager(broker, config_loader)
        runner = Runner(broker, strategy_manager)

        Logger.log(
            "系统",
            "系统启动",
            f"系统已就绪 (cycles={args.cycles}, max_seconds={args.max_seconds}, interval={args.interval})"
        )

        # 【修改点】使用字典解包 (**vars) 自动映射参数，消除冗余的手动对齐赋值
        runner.run(**vars(args))

    except KeyboardInterrupt:
        Logger.log("系统", "系统停止", "接收到操作系统的中断信号 (Ctrl+C)，正在安全退出...")
    except Exception as exc:
        # 【修改点】获取并打印完整的异常堆栈追踪，保留事故第一现场的绝对线索
        error_trace = traceback.format_exc()
        Logger.log("系统", "运行崩溃", f"主循环发生未捕获的严重异常: {exc}\n{error_trace}")
        return 1
    finally:
        # 确保无论是正常结束、Ctrl+C 还是崩溃，底层 API 资源都能被安全卸载
        broker.shutdown()
        Logger.log("系统", "系统停止", "底层终端连接已断开，资源释放完毕")

    return 0


if __name__ == "__main__":
    # 【修改点】使用更标准的 sys.exit 替代直接 raise SystemExit
    sys.exit(main())