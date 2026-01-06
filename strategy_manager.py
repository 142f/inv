import MetaTrader5 as mt5
from typing import Dict
from core.logger import Logger
from core.strategy_lib import GridStrategy


class StrategyManager:
    def __init__(self, mt5_client, config_loader):
        self.mt5_client = mt5_client
        self.config_loader = config_loader
        self.active: Dict[int, GridStrategy] = {}

    def sync(self):
        changed, configs = self.config_loader.load_if_changed()
        if not changed:
            return

        if configs is None:
            Logger.log("SYSTEM", "WARN", "配置加载结果为空")
            return

        if not configs:
            Logger.log("SYSTEM", "WARN", "配置文件为空或没有策略条目")

        new_magics = [cfg.get("magic") for cfg in configs if isinstance(cfg, dict)]

        for cfg in configs:
            magic = cfg.get("magic") if isinstance(cfg, dict) else None
            if magic is None:
                Logger.log("SYSTEM", "CONFIG_ERROR", "配置缺少 magic 字段，已跳过")
                continue

            if magic not in self.active:
                self._add_strategy(cfg)
            else:
                self._update_strategy(self.active[magic], cfg)

        for magic in list(self.active.keys()):
            if magic not in new_magics:
                self._remove_strategy(magic)

    def _add_strategy(self, cfg: dict):
        Logger.log("SYSTEM", "ADD", f"增加新策略: {cfg.get('symbol')} (Magic: {cfg.get('magic')})")
        strategy = GridStrategy(**cfg, lock=self.mt5_client.lock)
        self.active[cfg["magic"]] = strategy
        self.mt5_client.ensure_symbol(cfg["symbol"])
        strategy.clear_old_orders()

    def _update_strategy(self, strategy: GridStrategy, cfg: dict):
        current_state = strategy.get_state()

        new_symbol = cfg.get("symbol", strategy.symbol)
        if strategy.symbol != new_symbol:
            strategy.symbol = new_symbol
            self.mt5_client.ensure_symbol(strategy.symbol)
            Logger.log("SYSTEM", "UPDATE", f"策略 {strategy.magic} 品种变更为: {strategy.symbol}")

        strategy.enabled = cfg.get("enabled", strategy.enabled)
        strategy.step = cfg.get("step", strategy.step)
        strategy.tp_dist = cfg.get("tp_dist", strategy.tp_dist)
        strategy.lot = cfg.get("lot", strategy.lot)
        strategy.window = cfg.get("window", strategy.window)
        strategy.min_price = cfg.get("min_p", strategy.min_price)
        strategy.max_price = cfg.get("max_p", strategy.max_price)
        strategy.use_atr = cfg.get("use_atr", strategy.use_atr)
        strategy.atr_period = cfg.get("atr_period", strategy.atr_period)
        strategy.atr_factor = cfg.get("atr_factor", strategy.atr_factor)

        strategy.mode = cfg.get("mode", strategy.mode)
        strategy.out_of_range_action = cfg.get("out_of_range_action", strategy.out_of_range_action)
        strategy.buy_window = cfg.get("buy_window", strategy.buy_window)
        strategy.sell_window = cfg.get("sell_window", strategy.sell_window)

        strategy.recenter_steps = cfg.get("recenter_steps", strategy.recenter_steps)
        strategy.recenter_cooldown = cfg.get("recenter_cooldown", strategy.recenter_cooldown)
        strategy.max_long_pos = cfg.get("max_long_pos", strategy.max_long_pos)
        strategy.max_short_pos = cfg.get("max_short_pos", strategy.max_short_pos)
        strategy.max_long_vol = cfg.get("max_long_vol", strategy.max_long_vol)
        strategy.max_short_vol = cfg.get("max_short_vol", strategy.max_short_vol)
        strategy.max_net_vol = cfg.get("max_net_vol", strategy.max_net_vol)
        strategy.max_spread_points = cfg.get("max_spread_points", strategy.max_spread_points)
        strategy.extreme_mode = cfg.get("extreme_mode", strategy.extreme_mode)
        strategy.extreme_cooldown = cfg.get("extreme_cooldown", strategy.extreme_cooldown)
        strategy.max_new_orders_per_update = cfg.get(
            "max_new_orders_per_update", strategy.max_new_orders_per_update
        )

        strategy.hedge_enabled = cfg.get("hedge_enabled", strategy.hedge_enabled)
        strategy.hedge_fraction = float(cfg.get("hedge_fraction", strategy.hedge_fraction))
        strategy.hedge_tranches = int(cfg.get("hedge_tranches", strategy.hedge_tranches))
        strategy.hedge_entry_steps = int(cfg.get("hedge_entry_steps", strategy.hedge_entry_steps))
        strategy.hedge_exit_steps = int(cfg.get("hedge_exit_steps", strategy.hedge_exit_steps))
        strategy.hedge_cooldown = float(cfg.get("hedge_cooldown", strategy.hedge_cooldown))
        strategy.max_gross_vol = cfg.get("max_gross_vol", strategy.max_gross_vol)
        strategy.max_gross_vol = float(strategy.max_gross_vol) if strategy.max_gross_vol is not None else None

        strategy.hedge_vol_lookback = int(cfg.get("hedge_vol_lookback", strategy.hedge_vol_lookback))
        strategy.hedge_vol_window = int(cfg.get("hedge_vol_window", strategy.hedge_vol_window))
        strategy.hedge_vol_quantile = float(cfg.get("hedge_vol_quantile", strategy.hedge_vol_quantile))
        strategy.hedge_vol_base = int(cfg.get("hedge_vol_base", strategy.hedge_vol_base))
        strategy.hedge_vol_mult = float(cfg.get("hedge_vol_mult", strategy.hedge_vol_mult))

        strategy.be_trigger_steps = int(cfg.get("be_trigger_steps", strategy.be_trigger_steps))
        strategy.be_buffer_points = int(cfg.get("be_buffer_points", strategy.be_buffer_points))

        strategy.set_state(current_state)
        Logger.log("SYSTEM", "UPDATE", f"已同步策略状态: {strategy.symbol} (Enabled: {strategy.enabled})")

    def _remove_strategy(self, magic: int):
        strategy = self.active.pop(magic, None)
        if not strategy:
            return
        Logger.log("SYSTEM", "REMOVE", f"移除策略 Magic: {magic}")
        strategy.clear_old_orders()
