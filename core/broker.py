"""MT5 broker adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import threading
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5
from cryptography.fernet import Fernet
from dotenv import load_dotenv

from core.logger import Logger

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ── 凭证加解密（合并自 security.py）─────────────────────────────────────────────

class Security:
    def __init__(self, key_file: str = ".secret.key"):
        self.key_file = _PROJECT_ROOT / key_file
        self.key = self._load_or_create_key()
        self.cipher = Fernet(self.key)

    def _load_or_create_key(self):
        if self.key_file.exists():
            with open(self.key_file, "rb") as f:
                return f.read()

        key = Fernet.generate_key()
        with open(self.key_file, "wb") as f:
            f.write(key)

        try:
            os.chmod(self.key_file, 0o600)
        except Exception:
            pass

        return key

    def encrypt(self, text):
        if not text:
            return ""
        return self.cipher.encrypt(str(text).encode()).decode()

    def decrypt(self, encrypted_text):
        if not encrypted_text:
            return ""
        try:
            return self.cipher.decrypt(encrypted_text.encode()).decode()
        except Exception as exc:
            Logger.log("SYSTEM", "ERROR", f"解密失败({type(exc).__name__}): {exc!r}")
            return None


class BrokerBase(ABC):
    """Abstract base class for broker adapters."""

    @property
    @abstractmethod
    def lock(self) -> Any:
        """Shared lock for broker interactions (RLock recommended)."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if connected to the broker/terminal."""

    @abstractmethod
    def initialize(self) -> bool:
        """
        Initialize broker connection.
        Returns:
            bool: True if successful, False otherwise.
        """

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown broker connection and clean up resources."""

    @abstractmethod
    def ensure_symbol(self, symbol: str) -> bool:
        """
        Ensure a symbol is available and selected for data feed.
        """

    @abstractmethod
    def account_info(self) -> Any:
        """Return account info from broker."""

    @abstractmethod
    def terminal_info(self) -> Any:
        """Return terminal info from broker."""

    @abstractmethod
    def orders_get(self) -> Any:
        """Return active orders."""

    @abstractmethod
    def positions_get(self) -> Any:
        """Return active positions."""

    @abstractmethod
    def symbol_info_tick(self, symbol: str) -> Any:
        """Return latest tick for symbol."""

    @abstractmethod
    def order_send(self, request: dict) -> Any:
        """Send a trade request."""

    @abstractmethod
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        """Return rates from position."""


class MT5Broker(BrokerBase):
    def __init__(self) -> None:
        load_dotenv()
        self._lock = threading.RLock()
        self.security = Security()
        self._subscribed_symbols: set[str] = set()
        self._connected = False

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _decrypt_env(self, env_name: str, value: str | None) -> str:
        """Helper to safely decrypt env vars."""
        if not value:
            return ""
        if value.startswith("gAAAA"):
            decrypted = self.security.decrypt(value)
            if decrypted is None:
                Logger.log("SYSTEM", "WARN", f"{env_name} 解密失败，已忽略该配置项")
                return ""
            return decrypted
        return value

    def initialize(self) -> bool:
        # 解密环境变量
        acc_id_str = self._decrypt_env("MT5_ACCOUNT_ID", os.getenv("MT5_ACCOUNT_ID"))
        pwd = self._decrypt_env("MT5_PASSWORD", os.getenv("MT5_PASSWORD"))
        srv = self._decrypt_env("MT5_SERVER", os.getenv("MT5_SERVER"))
        mt5_path = self._decrypt_env("MT5_PATH", os.getenv("MT5_PATH"))

        acc_id = int(acc_id_str) if acc_id_str and acc_id_str.isdigit() else 0

        init_params: dict[str, Any] = {}
        if mt5_path:
            init_params["path"] = mt5_path

        with self._lock:
            # 1. 初始化终端（简化尝试逻辑）
            init_success = mt5.initialize(**init_params)
            if not init_success and mt5_path:
                # 如果指定路径失败，尝试默认路径（与原逻辑一致）
                init_success = mt5.initialize()
            if not init_success:
                Logger.log("SYSTEM", "ERROR", f"MT5 初始化失败: {mt5.last_error()}")
                return False

            # 检查终端信息是否可用
            if not mt5.terminal_info():
                Logger.log("SYSTEM", "ERROR", "MT5 初始化成功但终端信息不可用")
                mt5.shutdown()
                return False

            self._connected = True

            # 2. 获取当前账户信息（第一次调用）
            current_acc = mt5.account_info()
            need_login = False

            # 决定是否需要登录
            if acc_id != 0:
                if current_acc and current_acc.login == acc_id:
                    Logger.log("SYSTEM", "INFO", f"终端已登录目标账号 {acc_id}")
                else:
                    need_login = True
            else:
                if current_acc:
                    Logger.log("SYSTEM", "WARN", f"未指定账号，使用当前终端账号: {current_acc.login}")
                else:
                    Logger.log("SYSTEM", "ERROR", "未配置账号且当前终端未登录")
                    self.shutdown()
                    return False

            # 执行登录（如果需要）
            if need_login:
                Logger.log("SYSTEM", "INFO", f"正在尝试登录账号 {acc_id}...")
                if not mt5.login(acc_id, password=pwd, server=srv):
                    Logger.log("SYSTEM", "ERROR", f"登录失败: {mt5.last_error()} (Account: {acc_id})")
                    self.shutdown()
                    return False
                # 登录成功后重新获取账户信息（第二次调用）
                final_acc = mt5.account_info()
            else:
                # 无需登录，直接使用当前账户信息
                final_acc = current_acc

            # 3. 最终校验并记录日志
            if not final_acc:
                Logger.log("SYSTEM", "ERROR", "无法获取有效账户信息")
                self.shutdown()
                return False

            # 合并账户模式日志（原 _log_account_mode 内联）
            mode_map = {
                mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: "HEDGING (对冲模式)",
                mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING: "NETTING (净额模式)",
            }
            mode_str = mode_map.get(final_acc.margin_mode, f"模式 {final_acc.margin_mode}")
            Logger.log("SYSTEM", "INFO", f"账户模式: {mode_str}")
            if final_acc.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
                Logger.log("SYSTEM", "WARN", "当前策略非 HEDGING 设计，在 NETTING 模式下可能无法正确管理多层网格持仓。")

            Logger.log("SYSTEM", "INFO", f"MT5 就绪. 账号:{final_acc.login} @ {final_acc.server}")
            return True

    # _log_account_mode 方法已移除，功能内联到 initialize 中

    def shutdown(self) -> None:
        with self._lock:
            if self._connected:
                mt5.shutdown()
                self._connected = False
                Logger.log("SYSTEM", "SHUTDOWN", "MT5连接已关闭")

    def ensure_symbol(self, symbol: str) -> bool:
        if symbol in self._subscribed_symbols:
            return True

        with self._lock:
            info = mt5.symbol_info(symbol)
            if info and info.select:
                self._subscribed_symbols.add(symbol)
                return True

            if mt5.symbol_select(symbol, True):
                self._subscribed_symbols.add(symbol)
                return True

            Logger.log("SYSTEM", "ERROR", f"Symbol select failed: {symbol}")
            return False

    def account_info(self) -> Any:
        return mt5.account_info()

    def terminal_info(self) -> Any:
        return mt5.terminal_info()

    def orders_get(self) -> Any:
        return mt5.orders_get()

    def positions_get(self) -> Any:
        return mt5.positions_get()

    def symbol_info_tick(self, symbol: str) -> Any:
        return mt5.symbol_info_tick(symbol)

    def order_send(self, request: dict) -> Any:
        return mt5.order_send(request)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        return mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)