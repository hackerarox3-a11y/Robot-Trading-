"""
Script de diagnostic Deriv.
Teste la connexion ET l'autorisation pour identifier le probleme.
Usage: py test_deriv.py
"""

import asyncio
import json
import os
import sys

try:
    import websockets
except ImportError:
    print("ERREUR: Package 'websockets' non installe.")
    print("Fais: pip install websockets")
    sys.exit(1)


async def test_connection():
    print("=" * 50)
    print("  DIAGNOSTIC DERIV")
    print("=" * 50)

    # 1. Lire le config.json
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print("\n[ERREUR] Impossible de lire config.json:", e)
        return

    deriv = config.get("deriv", {})
    app_id = deriv.get("app_id", "NON TROUVE")
    token = os.getenv("DERIV_API_TOKEN", deriv.get("api_token", ""))
    ws_url = deriv.get("ws_url", "NON TROUVE")

    print("\n[1] Infos du config.json:")
    print("    app_id   =", app_id)
    print("    ws_url   =", ws_url)
    token_display = token if len(token) < 20 else token[:8] + "..." + token[-5:]
    print("    token    =", token_display, "(longueur=" + str(len(token)) + ")")

    has_space = " " in token
    has_quote = ('"' in token or "'" in token)
    has_newline = ("\n" in token or "\r" in token)
    print("    contient des espaces ?", has_space)
    print("    contient des guillemets ?", has_quote)
    print("    contient des sauts de ligne ?", has_newline)

    if not token:
        print("\n    [INFO] Aucun token Deriv configure. Definis DERIV_API_TOKEN avant le test.")
        return

    if has_space or has_quote or has_newline:
        print("")
        print("    !!! PROBLEME DETECTE !!!")
        print("    Ton token contient des caracteres invalides.")
        print("    Ouvre config.json et copie le token PROPREMENT.")
        return

    # 2. Test connexion WebSocket
    print("")
    print("[2] Test connexion WebSocket...")
    ws = None
    used_app_id = None
    for aid in [app_id, 36375, 22574, 1089, 1]:
        url = ws_url + "?app_id=" + str(aid)
        try:
            ws = await asyncio.wait_for(websockets.connect(url), timeout=10)
            used_app_id = aid
            print("    app_id=" + str(aid) + " -> CONNECTE")
            break
        except Exception as e:
            print("    app_id=" + str(aid) + " -> echoue:", e)

    if ws is None:
        print("")
        print("    Aucun app_id n'a fonctionne.")
        print("    Verifie ta connexion internet.")
        return

    # 3. Test autorisation
    print("")
    print("[3] Test autorisation avec le token...")
    await ws.send(json.dumps({"authorize": token}))
    try:
        response = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(response)

        if data.get("error"):
            err = data["error"]
            print("")
            print("    [ERREUR] Code:", err.get("code", "?"))
            print("    [ERREUR] Message:", err.get("message", "?"))
            print("")
            print("    === CE QUE TU DOIS FAIRE ===")
            print("")
            print("    1. Va sur https://app.deriv.com")
            print("    2. Connecte-toi a ton COMPTE REEL (pas demo)")
            print("    3. En bas a gauche > Parametres (icone engrenage)")
            print("    4. Clique sur 'API Token'")
            print("    5. Supprime tous les anciens tokens")
            print("    6. Clique 'Create New Token'")
            print("    7. Nom: Robot")
            print("    8. Coche UNIQUEMENT 'Trade' (pas Admin, pas Payments)")
            print("    9. Clique 'Create'")
            print("   10. COPIE le token en ENTIER (pas de login, juste le token)")
            print("   11. Dans config.json, colle-le entre les guillemets:")
            print('       "api_token": "COLLE_ICI_LE_TOKEN"')
            print("   12. Sauvegarde (Ctrl+S)")
        else:
            auth = data.get("authorize", {})
            print("")
            print("    [SUCCES] Compte:", auth.get("loginid"))
            print("    [SUCCES] Solde:", auth.get("balance"), auth.get("currency"))
            print("    [SUCCES] Email:", auth.get("email"))
            print("    [SUCCES] Pays:", auth.get("country"))
            print("    [SUCCES] Type de compte:", auth.get("is_virtual", "?"))
            print("")
            print("    === TON TOKEN FONCTIONNE ===")
            print("    Le robot est pret. Lance: py main.py --broker deriv")

    except Exception as e:
        print("    Erreur lors de l'autorisation:", e)

    if ws:
        await ws.close()

    print("")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(test_connection())
