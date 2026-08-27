# -*- coding: utf-8 -*-
"""
Utilitaires partages entre les modules du bot (sans dependance locale,
pour eviter tout import circulaire).
"""

from typing import Optional, Tuple


def normalize_lot(lot: float, min_lot: float, max_lot: float, lot_step: float) -> float:
    """
    Arrondit un volume au pas du symbole puis le borne entre min/max.
    Protege contre les valeurs invalides (lot_step <= 0 -> fallback sur min_lot).
    """
    try:
        step = float(lot_step)
    except (TypeError, ValueError):
        step = 0.0
    if step <= 0:
        step = float(min_lot) if min_lot and float(min_lot) > 0 else 0.01
    try:
        lo = max(0.0, float(min_lot))
        hi = float(max_lot)
    except (TypeError, ValueError):
        lo, hi = 0.0, max(step, 0.01)
    rounded = round(float(lot) / step) * step
    return max(lo, min(hi, rounded))


def feasibility_lot(raw_lot: float, min_lot: float, max_lot: float,
                    lot_step: float) -> Optional[float]:
    """
    Retourne le lot normalise si le lot theorique respecte la contrainte
    de gestion du risque face au volume minimum, sinon None.

    Règle P0 : si le lot nécessaire pour respecter le risque cible est
    INFÉRIEUR au lot minimum autorisé, il ne faut PAS trader (sinon le
    plancher imposerait un risque bien supérieur à la limite définie).
    """
    raw = max(0.0, float(raw_lot))
    lo = float(min_lot) if min_lot else 0.0
    # Tolérance de flottement (ex: 0.34999999 vs 0.35)
    if raw + 1e-9 < lo:
        return None
    return normalize_lot(raw, min_lot, max_lot, lot_step)


def spread_ok(bid: Optional[float], ask: Optional[float],
              max_spread_pips: float, pip_size: float) -> Tuple[bool, float]:
    """
    Vérifie que le spread actuel reste sous la limite configurable.
    Retourne (ok, spread_en_pips). Tolere des donnees incompletes
    (retourne True si bid/ask indisponibles afin de ne pas bloquer Deriv).
    """
    if bid is None or ask is None or pip_size <= 0:
        return True, 0.0
    spread_pips = abs(float(ask) - float(bid)) / pip_size
    if max_spread_pips is None or max_spread_pips <= 0:
        return True, spread_pips  # filtre desactive
    return spread_pips <= max_spread_pips, spread_pips


def protected_sl_tp(entry_price: float, signal: str,
                    sl_distance: float, tp_distance: float,
                    digits: int, min_stop_distance: float = 0.0) -> dict:
    """
    Construit les niveaux SL/TP proteges en unites de prix :
      - distances minimum enforcees contre le stops_level du broker
      - arrondi au nombre de chiffres du symbole

    Args:
        entry_price: prix d'entree (ask pour BUY, bid pour SELL)
        signal: 'BUY' ou 'SELL'
        sl_distance / tp_distance: distances cibles en unite de prix
        digits: nombre de decimales du symbole
        min_stop_distance: distance minimale imposee par le broker

    Returns:
        dict(sl=..., tp=..., sl_dist=..., tp_dist=...)
    """
    min_d = max(0.0, float(min_stop_distance))
    sl_d = max(float(sl_distance), min_d)
    tp_d = max(float(tp_distance), min_d)
    entry = float(entry_price)

    if str(signal).upper() == "BUY":
        sl = round(entry - sl_d, int(digits))
        tp = round(entry + tp_d, int(digits))
    else:
        sl = round(entry + sl_d, int(digits))
        tp = round(entry - tp_d, int(digits))

    return {
        "sl": sl, "tp": tp,
        "sl_dist": round(sl_d, int(digits)),
        "tp_dist": round(tp_d, int(digits)),
    }