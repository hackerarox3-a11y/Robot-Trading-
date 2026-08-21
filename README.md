# Trading Bot MT5 / Deriv

Bot de trading multi-broker pour Deriv et MetaTrader 5, avec mode simulation, analyse multi-timeframe et notifications Telegram.

## Installation

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Copier `.env.example` vers `.env`, renseigner les variables nécessaires, puis garder Telegram activé dans `config.json`. Le programme charge automatiquement `.env`; ne jamais remettre de token ou de mot de passe dans `config.json`.

## Vérification

```powershell
py -m unittest discover -s tests -v
py -m compileall -q .
```

Le test réseau Deriv est optionnel et nécessite `DERIV_API_TOKEN`:

```powershell
$env:DERIV_API_TOKEN = "votre_token"
py test_deriv.py
```

## Démarrage

```powershell
py main.py --dry-run --broker deriv
py main.py --broker mt5
```

Le compte réel et les accès API doivent être vérifiés avant tout démarrage sans `--dry-run`.