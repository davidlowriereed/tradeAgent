from __future__ import annotations
from dataclasses import dataclass
import time, math

@dataclass
class Position:
    side: str            # 'long'|'short'|'flat'
    qty: float = 0.0
    entry_px: float = 0.0
    entry_ts: float = 0.0

class PostureFSM:
    def __init__(self, risk_perc=0.0025, atr_k=1.2, max_hold_s=1800, cooldown_s=600):
        self.state = "flat"  # 'flat'|'long'|'short'
        self.pos = Position("flat", 0.0, 0.0, 0.0)
        self.last_exit_ts = 0.0
        self.risk_perc = risk_perc
        self.atr_k = atr_k
        self.max_hold_s = max_hold_s
        self.cooldown_s = cooldown_s
        self.daily_loss = 0.0
        self.daily_loss_limit = 0.01

    def _cooldown_ok(self, now): return (now - self.last_exit_ts) >= self.cooldown_s

    def size_from_atr(self, equity, atr):
        risk_budget = equity * self.risk_perc
        tick_risk = max(atr * self.atr_k, 1e-8)
        return max(risk_budget / tick_risk, 0.0)

    def enter(self, side: str, qty: float, px: float, now: float):
        self.state = side
        self.pos = Position(side, qty, px, now)

    def exit(self, px: float, now: float):
        # unrealized pnl → realized
        sign = 1 if self.pos.side == "long" else -1
        pnl = sign * (px - self.pos.entry_px) * self.pos.qty
        self.daily_loss += min(pnl, 0.0)
        self.last_exit_ts = now
        self.state = "flat"
        self.pos = Position("flat", 0.0, 0.0, 0.0)
        return pnl

    def step(self, now, equity, last_px, tf, trend_p_up, rules):
        """
        rules = dict with numeric thresholds:
          entry_up=0.65, exit_down=0.52, vwap_bps=15, rvol=1.3
        tf = signals_tf dict (has mom_bps_1m/5m/15m, px_vs_vwap_bps_1m, rvol_1m, atr_1m)
        """
        if self.daily_loss <= -equity*self.daily_loss_limit:
            return None  # halted

        if self.state == "flat":
            if not self._cooldown_ok(now): return None
            cond_long = (
              trend_p_up >= rules["entry_up"] and
              tf.get("mom_bps_1m",0) > 0 and tf.get("mom_bps_5m",0) > 0 and
              (tf.get("px_vs_vwap_bps_1m") or 0) >= rules["vwap_bps"] and
              (tf.get("rvol_1m") or 0) >= rules["rvol"]
            )
            cond_short = (
              trend_p_up <= (1-rules["entry_up"]) and
              tf.get("mom_bps_1m",0) < 0 and tf.get("mom_bps_5m",0) < 0 and
              (tf.get("px_vs_vwap_bps_1m") or 0) <= -rules["vwap_bps"] and
              (tf.get("rvol_1m") or 0) >= rules["rvol"]
            )
            if cond_long:
                qty = self.size_from_atr(equity, tf.get("atr_1m") or 0.0)
                self.enter("long", qty, last_px, now); return ("enter","long",qty,last_px)
            if cond_short:
                qty = self.size_from_atr(equity, tf.get("atr_1m") or 0.0)
                self.enter("short", qty, last_px, now); return ("enter","short",qty,last_px)
            return None

        # exits
        p = trend_p_up
        flip = (tf.get("px_vs_vwap_bps_1m") or 0)
        mom1 = tf.get("mom_bps_1m") or 0
        held = now - self.pos.entry_ts
        stop = self.pos.entry_px - self.atr_k*(tf.get("atr_1m") or 0.0) if self.state=="long" else self.pos.entry_px + self.atr_k*(tf.get("atr_1m") or 0.0)

        if self.state=="long":
            if p <= rules["exit_down"] or flip <= 0 or mom1 <= 0 or held >= self.max_hold_s or last_px <= stop:
                pnl = self.exit(last_px, now); return ("exit","long",self.pos.qty,last_px,pnl)
        else:
            if p >= (1-rules["exit_down"]) or flip >= 0 or mom1 >= 0 or held >= self.max_hold_s or last_px >= stop:
                pnl = self.exit(last_px, now); return ("exit","short",self.pos.qty,last_px,pnl)
        return None
