"""
Moteur d'analyse technique pour le robot de trading MetaTrader 5.
Calcule les indicateurs techniques : EMA, RSI, MACD, ADX, Bollinger Bands, ATR,
Stochastique, Williams %R, CCI, Ichimoku Cloud, Points Pivot,
MACD pondere par volume, Detection de divergence, Regime de volatilite.
"""

import numpy as np
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class TechnicalAnalysis:
    """
    Moteur d'analyse technique complet.
    Tous les indicateurs sont calcules a partir de tableaux numpy
    pour des performances optimales.
    """

    def __init__(self, config: dict):
        self.config = config
        ind = config["indicators"]
        self.ema_fast = ind["ema_fast"]
        self.ema_medium = ind["ema_medium"]
        self.ema_slow = ind["ema_slow"]
        self.ema_trend = ind["ema_trend"]
        self.rsi_period = ind["rsi_period"]
        self.rsi_overbought = ind["rsi_overbought"]
        self.rsi_oversold = ind["rsi_oversold"]
        self.macd_fast = ind["macd_fast"]
        self.macd_slow = ind["macd_slow"]
        self.macd_signal = ind["macd_signal"]
        self.adx_period = ind["adx_period"]
        self.adx_threshold = ind["adx_threshold"]
        self.bb_period = ind["bollinger_period"]
        self.bb_std = ind["bollinger_std"]
        self.atr_period = ind["atr_period"]

        # Parametres Stochastique (14, 3, 3 par defaut)
        self.stoch_k_period = ind.get("stoch_k_period", 14)
        self.stoch_d_period = ind.get("stoch_d_period", 3)
        self.stoch_smooth = ind.get("stoch_smooth", 3)

        # Parametres CCI (20 par defaut)
        self.cci_period = ind.get("cci_period", 20)

        # Parametres Williams %R (14 par defaut)
        self.williams_period = ind.get("williams_period", 14)

        # Parametres Ichimoku (9, 26, 52 par defaut)
        self.ichimoku_tenkan = ind.get("ichimoku_tenkan", 9)
        self.ichimoku_kijun = ind.get("ichimoku_kijun", 26)
        self.ichimoku_senkou = ind.get("ichimoku_senkou", 52)

    # ------------------------------------------------------------------
    #  INDICATEURS DE BASE
    # ------------------------------------------------------------------

    @staticmethod
    def ema(data: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average."""
        if data is None or len(data) == 0 or period < 1:
            return np.array([], dtype=float)
        if len(data) < period:
            return np.full_like(data, np.nan, dtype=float)
        ema = np.full_like(data, np.nan, dtype=float)
        vals = data.astype(float)
        start = 0
        count = 0
        s = 0.0
        for i in range(min(period, len(vals))):
            if not np.isnan(vals[i]):
                s += vals[i]
                count += 1
        if count == 0:
            return ema
        ema[period - 1] = s / count
        multiplier = 2.0 / (period + 1)
        for i in range(period, len(vals)):
            if np.isnan(vals[i]) or np.isnan(ema[i - 1]):
                ema[i] = ema[i - 1] if not np.isnan(ema[i - 1]) else np.nan
            else:
                ema[i] = (vals[i] - ema[i - 1]) * multiplier + ema[i - 1]
        return ema

    @staticmethod
    def sma(data: np.ndarray, period: int) -> np.ndarray:
        """Simple Moving Average."""
        if data is None or len(data) == 0 or period < 1:
            return np.array([], dtype=float)
        if len(data) < period:
            return np.full_like(data, np.nan, dtype=float)
        sma = np.full_like(data, np.nan, dtype=float)
        vals = data.astype(float)
        cumsum = np.nancumsum(vals)
        valid_count = np.zeros(len(vals), dtype=float)
        for i in range(len(vals)):
            if not np.isnan(vals[i]):
                valid_count[i] = 1.0
        cum_valid = np.cumsum(valid_count)
        for i in range(period - 1, len(vals)):
            if i == period - 1:
                window_sum = cumsum[i]
                window_count = cum_valid[i]
            else:
                window_sum = cumsum[i] - cumsum[i - period]
                window_count = cum_valid[i] - cum_valid[i - period]
            if window_count > 0:
                sma[i] = window_sum / window_count
        return sma

    def rsi(self, close: np.ndarray) -> np.ndarray:
        """Relative Strength Index."""
        if close is None or len(close) < self.rsi_period + 1:
            n = len(close) if close is not None else 0
            return np.full(n, np.nan, dtype=float)
        rsi = np.full_like(close, np.nan, dtype=float)
        period = self.rsi_period
        vals = close.astype(float)
        delta = np.diff(vals)
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.mean(gain[:period])
        avg_loss = np.mean(loss[:period])
        if avg_loss == 0:
            rsi[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[period] = 100.0 - (100.0 / (1.0 + rs))
        for i in range(period + 1, len(vals)):
            avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    def macd(self, close: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """MACD (line, signal, histogram)."""
        if close is None or len(close) < max(self.macd_slow, self.macd_fast) + self.macd_signal:
            n = len(close) if close is not None else 0
            nan_arr = np.full(n, np.nan, dtype=float)
            return nan_arr, nan_arr, nan_arr
        ema_fast = self.ema(close, self.macd_fast)
        ema_slow = self.ema(close, self.macd_slow)
        macd_line = ema_fast - ema_slow
        macd_clean = np.where(np.isnan(macd_line), 0.0, macd_line)
        signal_line = self.ema(macd_clean, self.macd_signal)
        signal_line = np.where(np.isnan(macd_line), np.nan, signal_line)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def adx(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Average Directional Index (+DI, -DI, ADX). Lissage Wilder."""
        period = self.adx_period
        n = len(close) if close is not None else 0
        if n < period * 2:
            nan_arr = np.full(n, np.nan, dtype=float)
            return nan_arr, nan_arr, nan_arr

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)

        tr = np.zeros(n)
        tr[0] = h[0] - l[0]
        for i in range(1, n):
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            up_move = h[i] - h[i - 1]
            down_move = l[i - 1] - l[i]
            if up_move > down_move and up_move > 0:
                plus_dm[i] = up_move
            if down_move > up_move and down_move > 0:
                minus_dm[i] = down_move

        atr = np.full(n, np.nan, dtype=float)
        plus_di = np.full(n, np.nan, dtype=float)
        minus_di = np.full(n, np.nan, dtype=float)
        adx_val = np.full(n, np.nan, dtype=float)

        atr[period] = np.mean(tr[1:period + 1])
        smooth_plus_dm = np.mean(plus_dm[1:period + 1])
        smooth_minus_dm = np.mean(minus_dm[1:period + 1])

        plus_di[period] = 100.0 * smooth_plus_dm / atr[period] if atr[period] > 1e-10 else 0.0
        minus_di[period] = 100.0 * smooth_minus_dm / atr[period] if atr[period] > 1e-10 else 0.0

        di_sum = plus_di[period] + minus_di[period]
        dx = 100.0 * abs(plus_di[period] - minus_di[period]) / di_sum if di_sum > 1e-10 else 0.0
        adx_val[period] = dx

        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            smooth_plus_dm = (smooth_plus_dm * (period - 1) + plus_dm[i]) / period
            smooth_minus_dm = (smooth_minus_dm * (period - 1) + minus_dm[i]) / period

            plus_di[i] = 100.0 * smooth_plus_dm / atr[i] if atr[i] > 1e-10 else 0.0
            minus_di[i] = 100.0 * smooth_minus_dm / atr[i] if atr[i] > 1e-10 else 0.0

            di_sum_i = plus_di[i] + minus_di[i]
            dx_i = 100.0 * abs(plus_di[i] - minus_di[i]) / di_sum_i if di_sum_i > 1e-10 else 0.0
            adx_val[i] = (adx_val[i - 1] * (period - 1) + dx_i) / period

        return plus_di, minus_di, adx_val

    def bollinger_bands(self, close: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Bollinger Bands (upper, middle, lower)."""
        n = len(close) if close is not None else 0
        if n < self.bb_period:
            nan_arr = np.full(n, np.nan, dtype=float)
            return nan_arr, nan_arr, nan_arr
        middle = self.sma(close, self.bb_period)
        std = np.full(n, np.nan, dtype=float)
        upper = np.full(n, np.nan, dtype=float)
        lower = np.full(n, np.nan, dtype=float)
        vals = close.astype(float)
        for i in range(self.bb_period - 1, n):
            window = vals[i - self.bb_period + 1:i + 1]
            valid = window[~np.isnan(window)]
            if len(valid) < 2:
                continue
            std[i] = np.std(valid, ddof=0)
            if not np.isnan(middle[i]):
                upper[i] = middle[i] + self.bb_std * std[i]
                lower[i] = middle[i] - self.bb_std * std[i]
        return upper, middle, lower

    def atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
        """Average True Range."""
        n = len(close) if close is not None else 0
        if n < self.atr_period + 1:
            return np.full(n, np.nan, dtype=float)
        atr_val = np.full(n, np.nan, dtype=float)
        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)
        tr = np.zeros(n)
        tr[0] = h[0] - l[0]
        for i in range(1, n):
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        atr_val[self.atr_period] = np.mean(tr[1:self.atr_period + 1])
        for i in range(self.atr_period + 1, n):
            atr_val[i] = (atr_val[i - 1] * (self.atr_period - 1) + tr[i]) / self.atr_period
        return atr_val

    # ------------------------------------------------------------------
    #  NOUVEAUX INDICATEURS
    # ------------------------------------------------------------------

    def stochastic_oscillator(self, high: np.ndarray, low: np.ndarray,
                               close: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Oscillateur Stochastique (%K et %D).
        Parametres par defaut : K=14, D=3, lissage=3.

        Returns:
            (stoch_k, stoch_d) - Lignes %K et %D
        """
        n = len(close) if close is not None else 0
        if n < self.stoch_k_period:
            nan_arr = np.full(n, np.nan, dtype=float)
            return nan_arr, nan_arr

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)
        k_period = self.stoch_k_period
        d_period = self.stoch_d_period
        smooth = self.stoch_smooth

        raw_k = np.full(n, np.nan, dtype=float)
        for i in range(k_period - 1, n):
            highest = np.nanmax(h[i - k_period + 1:i + 1])
            lowest = np.nanmin(l[i - k_period + 1:i + 1])
            if highest - lowest > 1e-10:
                raw_k[i] = ((c[i] - lowest) / (highest - lowest)) * 100.0
            else:
                raw_k[i] = 50.0

        stoch_k = self.sma(raw_k, smooth)
        stoch_d = self.sma(stoch_k, d_period)

        return stoch_k, stoch_d

    def williams_percent_r(self, high: np.ndarray, low: np.ndarray,
                            close: np.ndarray) -> np.ndarray:
        """
        Williams %R.
        Oscillateur de momentum entre -100 et 0.
        -80 et en dessous = survente, -20 et au-dessus = surachat.

        Returns:
            Tableau numpy des valeurs Williams %R
        """
        n = len(close) if close is not None else 0
        if n < self.williams_period:
            return np.full(n, np.nan, dtype=float)

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)
        period = self.williams_period
        williams = np.full(n, np.nan, dtype=float)

        for i in range(period - 1, n):
            highest = np.nanmax(h[i - period + 1:i + 1])
            lowest = np.nanmin(l[i - period + 1:i + 1])
            if highest - lowest > 1e-10:
                williams[i] = ((highest - c[i]) / (highest - lowest)) * -100.0
            else:
                williams[i] = -50.0

        return williams

    def cci(self, high: np.ndarray, low: np.ndarray,
            close: np.ndarray) -> np.ndarray:
        """
        Commodity Channel Index (CCI).
        CCI > 100 = surachat, CCI < -100 = survente.

        Returns:
            Tableau numpy des valeurs CCI
        """
        n = len(close) if close is not None else 0
        if n < self.cci_period:
            return np.full(n, np.nan, dtype=float)

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)
        period = self.cci_period
        cci_val = np.full(n, np.nan, dtype=float)

        tp = (h + l + c) / 3.0

        for i in range(period - 1, n):
            window = tp[i - period + 1:i + 1]
            mean_tp = np.nanmean(window)
            mean_dev = np.nanmean(np.abs(window - mean_tp))
            if mean_dev > 1e-10:
                cci_val[i] = (tp[i] - mean_tp) / (0.015 * mean_dev)
            else:
                cci_val[i] = 0.0

        return cci_val

    def volume_weighted_macd(self, close: np.ndarray,
                               volume: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        MACD pondere par le volume.
        Si le volume n'est pas disponible, retourne le MACD standard.

        Returns:
            (vw_macd_line, vw_signal_line, vw_histogram)
        """
        if close is None or len(close) < max(self.macd_slow, self.macd_fast) + self.macd_signal:
            n = len(close) if close is not None else 0
            nan_arr = np.full(n, np.nan, dtype=float)
            return nan_arr, nan_arr, nan_arr

        c = close.astype(float)

        if volume is None or len(volume) != len(c) or np.all(np.isnan(volume)):
            logger.debug("Volume non disponible, utilisation du MACD standard")
            return self.macd(c)

        v = volume.astype(float)
        valid_volumes = v[~np.isnan(v)]
        if len(valid_volumes) == 0:
            return self.macd(c)
        median_vol = np.median(valid_volumes)
        v = np.where(np.isnan(v), median_vol, v)
        v = np.where(v <= 0, median_vol, v)

        avg_vol = self.ema(v, 20)
        vol_ratio = np.where(avg_vol > 1e-10, v / avg_vol, 1.0)
        vol_ratio = np.clip(vol_ratio, 0.5, 2.0)
        weighted_close = c * vol_ratio

        ema_fast_w = self.ema(weighted_close, self.macd_fast)
        ema_slow_w = self.ema(weighted_close, self.macd_slow)
        vw_macd_line = ema_fast_w - ema_slow_w

        macd_clean = np.where(np.isnan(vw_macd_line), 0.0, vw_macd_line)
        vw_signal_line = self.ema(macd_clean, self.macd_signal)
        vw_signal_line = np.where(np.isnan(vw_macd_line), np.nan, vw_signal_line)
        vw_histogram = vw_macd_line - vw_signal_line

        return vw_macd_line, vw_signal_line, vw_histogram

    def ichimoku_cloud(self, high: np.ndarray, low: np.ndarray,
                       close: np.ndarray) -> Dict[str, np.ndarray]:
        """
        Nuage Ichimoku Kinko Hyo.
        Composantes : Tenkan-sen, Kijun-sen, Senkou Span A, Senkou Span B, Chikou Span.

        Returns:
            Dictionnaire avec les 5 composantes du nuage Ichimoku
        """
        n = len(close) if close is not None else 0
        nan_arr = np.full(n, np.nan, dtype=float)
        if n < self.ichimoku_senkou:
            return {
                "tenkan": nan_arr.copy(), "kijun": nan_arr.copy(),
                "senkou_a": nan_arr.copy(), "senkou_b": nan_arr.copy(),
                "chikou": nan_arr.copy(),
            }

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)

        tenkan = np.full(n, np.nan, dtype=float)
        kijun = np.full(n, np.nan, dtype=float)
        senkou_a = np.full(n, np.nan, dtype=float)
        senkou_b = np.full(n, np.nan, dtype=float)
        chikou = np.full(n, np.nan, dtype=float)

        tenkan_p = self.ichimoku_tenkan
        kijun_p = self.ichimoku_kijun
        senkou_p = self.ichimoku_senkou

        def _donchian_mid(arr_h: np.ndarray, arr_l: np.ndarray,
                          start: int, period: int) -> float:
            """Calcule le point milieu de Donchian."""
            end = start + 1
            s = max(0, start - period + 1)
            segment_h = arr_h[s:end]
            segment_l = arr_l[s:end]
            hi = np.nanmax(segment_h) if len(segment_h) > 0 else np.nan
            lo = np.nanmin(segment_l) if len(segment_l) > 0 else np.nan
            if np.isnan(hi) or np.isnan(lo):
                return np.nan
            return (hi + lo) / 2.0

        for i in range(n):
            if i >= tenkan_p - 1:
                val = _donchian_mid(h, l, i, tenkan_p)
                tenkan[i] = val if not np.isnan(val) else tenkan[i]

            if i >= kijun_p - 1:
                val = _donchian_mid(h, l, i, kijun_p)
                kijun[i] = val if not np.isnan(val) else kijun[i]

            if i >= kijun_p - 1:
                t = tenkan[i]
                k = kijun[i]
                if not np.isnan(t) and not np.isnan(k):
                    shifted = i + kijun_p
                    if shifted < n:
                        senkou_a[shifted] = (t + k) / 2.0

            if i >= senkou_p - 1:
                val = _donchian_mid(h, l, i, senkou_p)
                if not np.isnan(val):
                    shifted = i + kijun_p
                    if shifted < n:
                        senkou_b[shifted] = val

            if i >= kijun_p:
                chikou[i - kijun_p] = c[i]

        return {
            "tenkan": tenkan, "kijun": kijun,
            "senkou_a": senkou_a, "senkou_b": senkou_b,
            "chikou": chikou,
        }

    def pivot_points(self, high: np.ndarray, low: np.ndarray,
                     close: np.ndarray, timeframe: str = "daily") -> Dict[str, float]:
        """
        Calcule les points pivot classiques.
        Support et resistance levels (S1, S2, S3, R1, R2, R3).

        Args:
            high, low, close: Tableaux OHLC
            timeframe: 'daily' ou 'weekly'

        Returns:
            Dictionnaire avec PP, S1-S3, R1-R3
        """
        if high is None or low is None or close is None or len(close) < 2:
            return {}

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)

        if timeframe == "weekly":
            lookback = min(5, len(c))
        else:
            lookback = min(1, len(c))

        if lookback < 1:
            return {}

        recent_h = np.nanmax(h[-lookback: -1]) if lookback > 1 else h[-2]
        recent_l = np.nanmin(l[-lookback: -1]) if lookback > 1 else l[-2]
        recent_c = c[-2] if lookback == 1 else c[-(lookback + 1)]

        if np.isnan(recent_h) or np.isnan(recent_l) or np.isnan(recent_c):
            return {}

        pp = (recent_h + recent_l + recent_c) / 3.0

        return {
            "pp": float(pp),
            "r1": float(2.0 * pp - recent_l),
            "s1": float(2.0 * pp - recent_h),
            "r2": float(pp + (recent_h - recent_l)),
            "s2": float(pp - (recent_h - recent_l)),
            "r3": float(recent_h + 2.0 * (pp - recent_l)),
            "s3": float(recent_l - 2.0 * (recent_h - pp)),
        }

    # ------------------------------------------------------------------
    #  METHODES AVANCEES
    # ------------------------------------------------------------------

    def detect_divergence(self, close: np.ndarray,
                          rsi: np.ndarray, lookback: int = 30) -> Optional[str]:
        """
        Detecte la divergence RSI / Prix.

        Returns:
            'bullish', 'bearish', ou None
        """
        if close is None or rsi is None:
            return None
        n = len(close)
        if n < lookback or len(rsi) < lookback:
            return None

        c = close[-lookback:].astype(float)
        r = rsi[-lookback:].astype(float)

        valid_mask = ~np.isnan(c) & ~np.isnan(r)
        c = c[valid_mask]
        r = r[valid_mask]

        if len(c) < 10:
            return None

        price_min1_idx = None
        price_min2_idx = None
        price_max1_idx = None
        price_max2_idx = None

        for i in range(2, len(c) - 2):
            if c[i] < c[i - 1] and c[i] < c[i - 2] and c[i] < c[i + 1] and c[i] < c[i + 2]:
                if price_min2_idx is None:
                    price_min2_idx = i
                else:
                    price_min1_idx = price_min2_idx
                    price_min2_idx = i
            if c[i] > c[i - 1] and c[i] > c[i - 2] and c[i] > c[i + 1] and c[i] > c[i + 2]:
                if price_max2_idx is None:
                    price_max2_idx = i
                else:
                    price_max1_idx = price_max2_idx
                    price_max2_idx = i

        if (price_min1_idx is not None and price_min2_idx is not None
                and price_min2_idx > price_min1_idx):
            if (c[price_min2_idx] < c[price_min1_idx]
                    and r[price_min2_idx] > r[price_min1_idx]):
                logger.info("Divergence haussiere detectee (RSI vs prix)")
                return "bullish"

        if (price_max1_idx is not None and price_max2_idx is not None
                and price_max2_idx > price_max1_idx):
            if (c[price_max2_idx] > c[price_max1_idx]
                    and r[price_max2_idx] < r[price_max1_idx]):
                logger.info("Divergence baissiere detectee (RSI vs prix)")
                return "bearish"

        return None

    def get_volatility_regime(self, atr: np.ndarray,
                              close: np.ndarray) -> str:
        """
        Determine le regime de volatilite actuel base sur le percentile de l'ATR.

        Returns:
            'low', 'medium', 'high', 'extreme', ou 'unknown'
        """
        if atr is None or close is None:
            return "unknown"
        n = len(atr)
        if n < 20:
            return "unknown"

        c = close.astype(float)
        a = atr.astype(float)
        valid = ~np.isnan(a) & ~np.isnan(c) & (c > 1e-10)
        if np.sum(valid) < 10:
            return "unknown"

        atr_pct = a[valid] / c[valid] * 100.0
        current_atr_pct = atr_pct[-1]

        p25 = float(np.percentile(atr_pct, 25))
        p50 = float(np.percentile(atr_pct, 50))
        p75 = float(np.percentile(atr_pct, 75))

        if current_atr_pct <= p25:
            regime = "low"
        elif current_atr_pct <= p50:
            regime = "medium"
        elif current_atr_pct <= p75:
            regime = "high"
        else:
            regime = "extreme"

        logger.debug(
            f"Regime volatilite : {regime} | "
            f"ATR%={current_atr_pct:.4f} | "
            f"P25={p25:.4f} P50={p50:.4f} P75={p75:.4f}"
        )
        return regime

    # ------------------------------------------------------------------
    #  ANALYSE COMPLETE
    # ------------------------------------------------------------------

    def full_analysis(self, ohlc_data: Dict[str, np.ndarray]) -> Dict:
        """
        Lance tous les indicateurs et retourne un dictionnaire complet.

        ohlc_data doit contenir : 'open', 'high', 'low', 'close'
        Optionnel : 'volume' pour le MACD pondere
        """
        close = ohlc_data["close"]
        high = ohlc_data["high"]
        low = ohlc_data["low"]
        volume = ohlc_data.get("volume")

        ema_f = self.ema(close, self.ema_fast)
        ema_m = self.ema(close, self.ema_medium)
        ema_s = self.ema(close, self.ema_slow)
        ema_t = self.ema(close, self.ema_trend)

        rsi_val = self.rsi(close)

        macd_line, signal_line, histogram = self.macd(close)

        vw_macd_line, vw_signal_line, vw_histogram = self.volume_weighted_macd(
            close, volume
        )

        plus_di, minus_di, adx_val = self.adx(high, low, close)

        bb_upper, bb_middle, bb_lower = self.bollinger_bands(close)

        atr_val = self.atr(high, low, close)

        stoch_k, stoch_d = self.stochastic_oscillator(high, low, close)

        williams = self.williams_percent_r(high, low, close)

        cci_val = self.cci(high, low, close)

        ichimoku = self.ichimoku_cloud(high, low, close)

        pivots_daily = self.pivot_points(high, low, close, "daily")
        pivots_weekly = self.pivot_points(high, low, close, "weekly")

        vol_regime = self.get_volatility_regime(atr_val, close)

        divergence = self.detect_divergence(close, rsi_val)

        result = {
            "ema_fast": ema_f, "ema_medium": ema_m,
            "ema_slow": ema_s, "ema_trend": ema_t,
            "rsi": rsi_val,
            "macd_line": macd_line, "macd_signal": signal_line, "macd_hist": histogram,
            "vw_macd_line": vw_macd_line, "vw_macd_signal": vw_signal_line, "vw_macd_hist": vw_histogram,
            "plus_di": plus_di, "minus_di": minus_di, "adx": adx_val,
            "bb_upper": bb_upper, "bb_middle": bb_middle, "bb_lower": bb_lower,
            "atr": atr_val,
            "stoch_k": stoch_k, "stoch_d": stoch_d,
            "williams_r": williams,
            "cci": cci_val,
            "ichimoku_tenkan": ichimoku["tenkan"],
            "ichimoku_kijun": ichimoku["kijun"],
            "ichimoku_senkou_a": ichimoku["senkou_a"],
            "ichimoku_senkou_b": ichimoku["senkou_b"],
            "ichimoku_chikou": ichimoku["chikou"],
            "pivots_daily": pivots_daily,
            "pivots_weekly": pivots_weekly,
            "volatility_regime": vol_regime,
            "divergence": divergence,
        }
        return result

    def get_latest_values(self, analysis: Dict) -> Dict:
        """
        Extrait les dernieres valeurs valides de chaque indicateur.
        Inclut tous les indicateurs y compris les nouveaux.
        """
        latest: Dict = {}
        for key, arr in analysis.items():
            if isinstance(arr, np.ndarray):
                valid = arr[~np.isnan(arr)]
                latest[key] = float(valid[-1]) if len(valid) > 0 else None
            elif isinstance(arr, dict) and key.startswith("pivots_"):
                latest[key] = arr
            elif key in ("volatility_regime", "divergence"):
                latest[key] = arr
        return latest
