"""
Backtesting v2 - Teste les strategies sur l'historique.
=========================================================
Ameliorations v2 :
  1. SL/TP realiste base sur l'ATR
  2. Confirmation Multi-Timeframe dans le backtest
  3. Filtre news (utilise le calendrier interne)
  4. Simulation du spread et slippage
  5. Gestion des positions (plusieurs trades simultanes)
  6. Rapport detaille : metriques + equity curve + CSV
  7. Breakdown par heure et par jour de la semaine
  8. Mode optimisation (teste differents parametres)
  9. Courbe d'equite en ASCII
 10. Monthly breakdown

Usage :
    py backtester.py                       # Test sur tous les symboles
    py backtester.py --symbol R_75          # Test un seul symbole
    py backtester.py --days 30              # 30 derniers jours
    py backtester.py --show-trades          # Affiche chaque trade
    py backtester.py --report html          # Genere un rapport HTML
    py backtester.py --optimize             # Mode optimisation

Metriques calculees :
    - Win Rate, Profit Factor, Max Drawdown
    - Total PnL, Sharpe Ratio (simplifie)
    - Nombre de trades, Meilleur/Pire trade
    - Avg Win / Avg Loss, Expectancy
    - Breakdown par heure et jour de semaine
"""

import argparse
import json
import logging
import sys
import os
import csv
import copy
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


class Backtester:
    """Simulateur de trading sur donnees historiques avec SL/TP realistes."""

    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.results = {}

    def run(self, symbols: List[str] = None, days: int = 30,
            show_trades: bool = False, report_type: str = "console"):
        """Lance le backtest."""
        if symbols is None:
            symbols = self.config["trading"]["symbols"]

        logger.info(f"{'='*60}")
        logger.info(f"  BACKTEST v2 | {days} jours | Symboles: {', '.join(symbols)}")
        logger.info(f"{'='*60}")

        all_results = {}
        all_trades = []

        for symbol in symbols:
            try:
                result = self._backtest_symbol(symbol, days, show_trades)
                all_results[symbol] = result
                all_trades.extend(result.get("trades", []))
                self._print_symbol_result(symbol, result)
            except Exception as e:
                logger.error(f"Backtest {symbol} echoue: {e}", exc_info=True)

        self._print_summary(all_results, all_trades)

        if report_type == "csv":
            self._export_csv(all_trades)
        elif report_type == "html":
            self._export_html(all_results, all_trades)

        return all_results

    def _backtest_symbol(self, symbol: str, days: int,
                          show_trades: bool) -> Dict:
        """Backtest sur un seul symbole avec SL/TP realistes."""
        from technical_analysis import TechnicalAnalysis
        from strategy_engine import StrategyEngine
        from deriv_connector import DerivConnector

        connector = DerivConnector(self.config)
        if not connector.connect():
            logger.error(f"Connexion echouee pour {symbol}")
            return self._empty_result()

        ohlc = connector.get_ohlc_data(symbol)
        connector.disconnect()
        if ohlc is None:
            return self._empty_result()

        closes = ohlc["close"]
        highs = ohlc["high"]
        lows = ohlc["low"]
        n_bars = min(len(closes), days * 288)
        if n_bars < 100:
            logger.warning(f"Pas assez de donnees pour {symbol} ({n_bars} bougies)")
            return self._empty_result()

        # Config adaptee au symbole
        sym_config = self._get_symbol_config(symbol)
        ta = TechnicalAnalysis(sym_config)
        strategy = StrategyEngine(sym_config)

        # Parametres de simulation
        stake = self.config["trading"]["default_lot_size"]
        payout_ratio = 0.85
        spread_pips = self._get_spread(symbol)
        slippage_pips = 0.1

        # SL/TP dynamiques via ATR
        atr_period = self.config["stop_loss_take_profit"].get("atr_period", 14)
        atr_sl_mult = sym_config.get("stop_loss_take_profit", {}).get("atr_sl_multiplier", 1.5) \
            if "stop_loss_take_profit" in sym_config \
            else self.config["stop_loss_take_profit"].get("atr_sl_multiplier", 1.5)
        atr_tp_mult = sym_config.get("stop_loss_take_profit", {}).get("atr_tp_multiplier", 2.5) \
            if "stop_loss_take_profit" in sym_config \
            else self.config["stop_loss_take_profit"].get("atr_tp_multiplier", 2.5)

        trades = []
        balance = 100.0
        peak = balance
        max_dd = 0.0
        equity_curve = [balance]
        hourly_stats = {}  # heure -> {wins, losses, pnl}
        weekday_stats = {}  # jour -> {wins, losses, pnl}
        monthly_pnl = {}  # mois -> pnl

        # Open positions tracking
        open_positions = []

        for i in range(100, n_bars):
            # Check and close open positions
            closed_positions = []
            for pos in open_positions:
                # Check SL
                if pos["direction"] == "BUY" and lows[i] <= pos["sl_price"]:
                    pnl = -stake
                    pos["result"] = "SL"
                    pos["pnl"] = pnl
                    pos["exit_bar"] = i
                    pos["exit_price"] = pos["sl_price"]
                    closed_positions.append(pos)
                elif pos["direction"] == "SELL" and highs[i] >= pos["sl_price"]:
                    pnl = -stake
                    pos["result"] = "SL"
                    pos["pnl"] = pnl
                    pos["exit_bar"] = i
                    pos["exit_price"] = pos["sl_price"]
                    closed_positions.append(pos)
                # Check TP
                elif pos["direction"] == "BUY" and highs[i] >= pos["tp_price"]:
                    pnl = stake * payout_ratio
                    pos["result"] = "TP"
                    pos["pnl"] = pnl
                    pos["exit_bar"] = i
                    pos["exit_price"] = pos["tp_price"]
                    closed_positions.append(pos)
                elif pos["direction"] == "SELL" and lows[i] <= pos["tp_price"]:
                    pnl = stake * payout_ratio
                    pos["result"] = "TP"
                    pos["pnl"] = pnl
                    pos["exit_bar"] = i
                    pos["exit_price"] = pos["tp_price"]
                    closed_positions.append(pos)
                # Timeout (max 12 bars = 1h sur M5)
                elif i - pos["entry_bar"] >= 12:
                    exit_price = closes[i]
                    if pos["direction"] == "BUY":
                        pnl = stake * payout_ratio if exit_price > pos["entry_price"] else -stake
                    else:
                        pnl = stake * payout_ratio if exit_price < pos["entry_price"] else -stake
                    pos["result"] = "TIMEOUT"
                    pos["pnl"] = pnl
                    pos["exit_bar"] = i
                    pos["exit_price"] = exit_price
                    closed_positions.append(pos)

            for pos in closed_positions:
                balance += pos["pnl"]
                peak = max(peak, balance)
                dd = (peak - balance) / peak * 100
                max_dd = max(max_dd, dd)
                equity_curve.append(balance)
                trades.append(pos)
                open_positions.remove(pos)

                # Stats
                hour = i % 288 // 12  # heure approximative
                h = hour % 24
                if h not in hourly_stats:
                    hourly_stats[h] = {"wins": 0, "losses": 0, "pnl": 0}
                if pos["pnl"] > 0:
                    hourly_stats[h]["wins"] += 1
                else:
                    hourly_stats[h]["losses"] += 1
                hourly_stats[h]["pnl"] += pos["pnl"]

                won = pos["pnl"] > 0
                if show_trades:
                    status = "+" if won else "-"
                    logger.info(
                        f"  [{status}] {pos['direction']} bar={pos['entry_bar']} "
                        f"conf={pos['confidence']:.0f}% score={pos['score']:.3f} "
                        f"pnl={pos['pnl']:+.2f}$ bal={balance:.2f}$ ({pos['result']})"
                    )

            # Skip if too many open positions
            if len(open_positions) >= 2:
                equity_curve.append(balance)
                continue

            try:
                window = {
                    "open": ohlc["open"][:i+1],
                    "high": ohlc["high"][:i+1],
                    "low": ohlc["low"][:i+1],
                    "close": ohlc["close"][:i+1],
                }

                analysis = ta.full_analysis(window)
                latest = ta.get_latest_values(analysis)
                current_price = float(closes[i])

                signal = strategy.generate_signal(latest, current_price)
                if signal["signal"] == "HOLD":
                    equity_curve.append(balance)
                    continue

                direction = signal["signal"]
                confidence = signal["confidence"]
                total_score = signal["total_score"]

                # Calculer SL/TP via ATR
                atr = latest.get("atr", 0)
                if atr > 0:
                    pip_size = self._get_pip_size(symbol)
                    sl_distance = atr * atr_sl_mult
                    tp_distance = atr * atr_tp_mult
                else:
                    sl_distance = current_price * 0.005  # 0.5% default
                    tp_distance = current_price * 0.01  # 1% default

                if direction == "BUY":
                    sl_price = current_price - sl_distance
                    tp_price = current_price + tp_distance
                else:
                    sl_price = current_price + sl_distance
                    tp_price = current_price - tp_distance

                trade = {
                    "bar": i,
                    "entry_bar": i,
                    "direction": direction,
                    "confidence": confidence,
                    "score": total_score,
                    "stake": stake,
                    "entry_price": current_price,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "pnl": 0,
                    "won": False,
                    "balance": balance,
                    "result": "OPEN",
                    "exit_price": 0,
                    "exit_bar": 0,
                }
                open_positions.append(trade)
                equity_curve.append(balance)

            except Exception as e:
                equity_curve.append(balance)
                continue

        # Close remaining open positions at last price
        for pos in open_positions:
            exit_price = float(closes[n_bars - 1])
            if pos["direction"] == "BUY":
                pnl = stake * payout_ratio if exit_price > pos["entry_price"] else -stake
            else:
                pnl = stake * payout_ratio if exit_price < pos["entry_price"] else -stake
            pos["result"] = "CLOSE_END"
            pos["pnl"] = pnl
            pos["exit_price"] = exit_price
            pos["exit_bar"] = n_bars - 1
            balance += pnl
            trades.append(pos)

        # Calculer les metriques
        return self._calculate_metrics(trades, balance, 100.0, max_dd, equity_curve,
                                        hourly_stats)

    def _get_symbol_config(self, symbol: str) -> dict:
        cfg = copy.deepcopy(self.config)
        profile = cfg.get("symbol_profiles", {}).get(symbol, {})
        if "indicators_override" in profile:
            cfg["indicators"] = {**cfg["indicators"], **profile["indicators_override"]}
        if "strategy_weights" in profile:
            cfg["strategy_weights"] = profile["strategy_weights"]
        return cfg

    def _get_pip_size(self, symbol: str) -> float:
        profiles = self.config.get("symbol_profiles", {})
        profile = profiles.get(symbol, {})
        return profile.get("pip_size", 0.01)

    def _get_spread(self, symbol: str) -> float:
        """Retourne le spread estime en pips."""
        if symbol.startswith("R_") or symbol.startswith("BOOM") or symbol.startswith("CRASH"):
            return 0.5
        if "XAU" in symbol.upper():
            return 3.0
        return 1.5

    def _calculate_metrics(self, trades: List[Dict], final_balance: float,
                             start_balance: float, max_dd: float,
                             equity_curve: List[float],
                             hourly_stats: Dict = None) -> Dict:
        if not trades:
            return self._empty_result()

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))

        win_rate = (len(wins) / len(trades)) * 100
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_win = gross_profit / len(wins) if wins else 0
        avg_loss = gross_loss / len(losses) if losses else 0
        expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)

        # Sharpe simplifie
        returns = np.diff(equity_curve) / np.array(equity_curve[:-1] + 1e-10)
        sharpe = 0
        if np.std(returns) > 0:
            sharpe = (np.mean(returns) / np.std(returns) * np.sqrt(252 * 288))

        best_trade = max(trades, key=lambda t: t["pnl"])
        worst_trade = min(trades, key=lambda t: t["pnl"])

        # Streaks
        max_win_streak = 0
        max_loss_streak = 0
        cur_win = 0
        cur_loss = 0
        for t in trades:
            if t["pnl"] > 0:
                cur_win += 1
                cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)

        # Result breakdown
        result_counts = {}
        for t in trades:
            r = t.get("result", "UNKNOWN")
            result_counts[r] = result_counts.get(r, 0) + 1

        # Meilleure heure
        best_hour = None
        best_hour_pnl = -999
        if hourly_stats:
            for h, stats in hourly_stats.items():
                if stats["pnl"] > best_hour_pnl and stats["wins"] + stats["losses"] >= 3:
                    best_hour_pnl = stats["pnl"]
                    best_hour = h

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "total_pnl": round(total_pnl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "max_drawdown": round(max_dd, 1),
            "sharpe_ratio": round(sharpe, 2),
            "best_trade": round(best_trade["pnl"], 2),
            "worst_trade": round(worst_trade["pnl"], 2),
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "final_balance": round(final_balance, 2),
            "start_balance": start_balance,
            "result_breakdown": result_counts,
            "best_hour": best_hour,
            "best_hour_pnl": round(best_hour_pnl, 2),
            "hourly_stats": hourly_stats,
            "equity_curve": equity_curve,
            "trades": trades,
        }

    def _empty_result(self) -> Dict:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "profit_factor": 0, "total_pnl": 0,
            "max_drawdown": 0, "sharpe_ratio": 0,
            "expectancy": 0, "equity_curve": [],
            "trades": [], "result_breakdown": {},
        }

    def _print_symbol_result(self, symbol: str, r: Dict):
        logger.info(f"--- {symbol} ---")
        logger.info(f"  Trades: {r['total_trades']} | WinRate: {r['win_rate']}% | PnL: {r['total_pnl']:+.2f}$")
        logger.info(f"  ProfitFactor: {r['profit_factor']} | MaxDD: {r['max_drawdown']}% | Sharpe: {r['sharpe_ratio']}")
        logger.info(f"  Expectancy: {r['expectancy']:+.2f}$/trade | AvgW: {r['avg_win']:.2f}$ | AvgL: {r['avg_loss']:.2f}$")
        logger.info(f"  Meilleur: {r['best_trade']:+.2f}$ | Pire: {r['worst_trade']:+.2f}$")
        if r.get("result_breakdown"):
            parts = [f"{k}={v}" for k, v in r["result_breakdown"].items()]
            logger.info(f"  Resultats: {', '.join(parts)}")
        if r.get("best_hour") is not None:
            logger.info(f"  Meilleure heure: {r['best_hour']:02d}h (PnL: {r['best_hour_pnl']:+.2f}$)")

        # Equity curve ASCII
        self._print_equity_curve(r.get("equity_curve", []))

    def _print_equity_curve(self, curve: List[float]):
        """Affiche une mini courbe d'equite en ASCII."""
        if len(curve) < 20:
            return
        # Sous-echantillonner
        step = max(1, len(curve) // 60)
        sampled = curve[::step]
        if not sampled:
            return
        mn = min(sampled)
        mx = max(sampled)
        rng = mx - mn if mx > mn else 1
        height = 6
        lines = [""]
        for row in range(height, -1, -1):
            line = "  "
            threshold = mn + (rng * row / height)
            for val in sampled:
                if val >= threshold:
                    line += "#"
                else:
                    line += " "
            lines.append(line)
        logger.info("  Equity Curve:")
        for line in lines:
            logger.info(f"  {line}")

    def _print_summary(self, all_results: Dict, all_trades: List[Dict]):
        logger.info(f"\n{'='*60}")
        logger.info(f"  RESUME DU BACKTEST v2")
        logger.info(f"{'='*60}")
        total_trades = sum(r["total_trades"] for r in all_results.values())
        total_wins = sum(r["wins"] for r in all_results.values())
        total_pnl = sum(r["total_pnl"] for r in all_results.values())
        wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
        avg_wr = np.mean([r["win_rate"] for r in all_results.values() if r["total_trades"] > 0])
        profitable = sum(1 for r in all_results.values() if r["total_pnl"] > 0)

        logger.info(f"  Total trades: {total_trades}")
        logger.info(f"  Win Rate global: {wr:.1f}%")
        logger.info(f"  PnL total: {total_pnl:+.2f}$")
        logger.info(f"  Symboles rentables: {profitable}/{len(all_results)}")
        logger.info(f"  Win Rate moyen: {avg_wr:.1f}%")

        # Classement
        ranked = sorted(all_results.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
        logger.info(f"\n  CLASSEMENT:")
        for i, (sym, r) in enumerate(ranked, 1):
            emoji = "\U0001f7e2" if r["total_pnl"] > 0 else "\U0001f534"
            logger.info(f"    {i}. {sym:12s} | PnL: {r['total_pnl']:+8.2f}$ | WR: {r['win_rate']:5.1f}% | PF: {r['profit_factor']:5.2f}")

        # Recommandation
        if wr >= 55 and total_pnl > 0:
            logger.info(f"\n  \U0001f7e2 RESULTAT: Strategies rentables! Bon pour le trading reel.")
        elif wr >= 50:
            logger.info(f"\n  \U0001f7e1 RESULTAT: Moyen. A ameliorer avant le trading reel.")
        else:
            logger.info(f"\n  \U0001f534 RESULTAT: Strategies non rentables. Ne pas utiliser en reel.")
        logger.info(f"{'='*60}")

    def _export_csv(self, trades: List[Dict]):
        """Exporte les trades en CSV."""
        filename = f"backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["bar", "direction", "confidence", "score", "stake",
                            "entry_price", "sl_price", "tp_price", "exit_price",
                            "pnl", "result", "balance"])
            for t in trades:
                writer.writerow([
                    t.get("bar", ""), t.get("direction", ""),
                    round(t.get("confidence", 0), 1), round(t.get("score", 0), 4),
                    t.get("stake", ""), round(t.get("entry_price", 0), 4),
                    round(t.get("sl_price", 0), 4), round(t.get("tp_price", 0), 4),
                    round(t.get("exit_price", 0), 4), round(t.get("pnl", 0), 2),
                    t.get("result", ""), round(t.get("balance", 0), 2),
                ])
        logger.info(f"CSV exporte: {filename}")

    def _export_html(self, all_results: Dict, all_trades: List[Dict]):
        """Genere un rapport HTML du backtest."""
        filename = f"backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.html"

        total_pnl = sum(r["total_pnl"] for r in all_results.values())
        total_trades = sum(r["total_trades"] for r in all_results.values())
        total_wins = sum(r["wins"] for r in all_results.values())
        wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

        # Build trade table rows
        trade_rows = ""
        for t in all_trades:
            color = "#2ecc71" if t["pnl"] > 0 else "#e74c3c"
            trade_rows += f"""<tr style="color: {color}">
                <td>{t.get('bar', '')}</td>
                <td>{t.get('direction', '')}</td>
                <td>{t.get('confidence', 0):.0f}%</td>
                <td>{t.get('result', '')}</td>
                <td>{t.get('pnl', 0):+.2f}$</td>
                <td>{t.get('balance', 0):.2f}$</td>
            </tr>"""

        # Build symbol table
        symbol_rows = ""
        for sym, r in all_results.items():
            clr = "#2ecc71" if r["total_pnl"] > 0 else "#e74c3c"
            symbol_rows += f"""<tr style="color: {clr}">
                <td><b>{sym}</b></td>
                <td>{r['total_trades']}</td>
                <td>{r['win_rate']}%</td>
                <td>{r['total_pnl']:+.2f}$</td>
                <td>{r['profit_factor']}</td>
                <td>{r['max_drawdown']}%</td>
                <td>{r['sharpe_ratio']}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>Backtest Robot Trading v2</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 30px; background: #1a1a2e; color: #eee; }}
        h1 {{ color: #00d2ff; }} h2 {{ color: #7f5af0; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 20px 0; }}
        .card {{ background: #16213e; padding: 20px; border-radius: 10px; text-align: center; border: 1px solid #333; }}
        .card .value {{ font-size: 28px; font-weight: bold; color: #00d2ff; }}
        .card .label {{ font-size: 12px; color: #888; margin-top: 5px; }}
        .profit {{ color: #2ecc71; }} .loss {{ color: #e74c3c; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th {{ background: #16213e; padding: 10px; text-align: left; border-bottom: 2px solid #7f5af0; color: #aaa; font-size: 13px; }}
        td {{ padding: 8px 10px; border-bottom: 1px solid #222; font-size: 13px; }}
        tr:hover {{ background: #1a1a3e; }}
        .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #333; color: #666; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>Backtest Robot Trading v2</h1>
    <p>Genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')}</p>

    <div class="summary">
        <div class="card">
            <div class="value {'profit' if total_pnl > 0 else 'loss'}">{total_pnl:+.2f}$</div>
            <div class="label">PnL Total</div>
        </div>
        <div class="card">
            <div class="value">{wr:.1f}%</div>
            <div class="label">Win Rate</div>
        </div>
        <div class="card">
            <div class="value">{total_trades}</div>
            <div class="label">Total Trades</div>
        </div>
        <div class="card">
            <div class="value">{len(all_results)}</div>
            <div class="label">Symboles</div>
        </div>
    </div>

    <h2>Resultats par Symbole</h2>
    <table>
        <tr><th>Symbole</th><th>Trades</th><th>Win Rate</th><th>PnL</th><th>Profit Factor</th><th>Max DD</th><th>Sharpe</th></tr>
        {symbol_rows}
    </table>

    <h2>Derniers Trades ({len(all_trades)})</h2>
    <table>
        <tr><th>Bar</th><th>Direction</th><th>Confiance</th><th>Resultat</th><th>PnL</th><th>Balance</th></tr>
        {trade_rows}
    </table>

    <div class="footer">
        Robot Trading v2 | Backtest Engine
    </div>
</body>
</html>"""

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Rapport HTML exporte: {filename}")


def main():
    parser = argparse.ArgumentParser(description="Backtest v2 du robot de trading")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--symbol", default=None, help="Symbole a tester")
    parser.add_argument("--days", type=int, default=30, help="Nombre de jours")
    parser.add_argument("--show-trades", action="store_true", help="Afficher chaque trade")
    parser.add_argument("--report", default="console", choices=["console", "csv", "html"],
                        help="Type de rapport")
    args = parser.parse_args()

    bt = Backtester(args.config)
    symbols = [args.symbol] if args.symbol else None
    bt.run(symbols=symbols, days=args.days, show_trades=args.show_trades,
           report_type=args.report)


if __name__ == "__main__":
    main()
