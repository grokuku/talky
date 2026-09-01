# -*- coding: utf-8 -*-
"""
main.py
=======
Point d'entrée de « Talky » (dictée vocale client/serveur, CachyOS).

Lancement :
    python main.py
    # ou : uvicorn "app.api.factory:build_app" --factory
"""

import uvicorn

from app.api.factory import build_app

app = build_app()

if __name__ == "__main__":
    print("=" * 60)
    print("  Talky - Dictée vocale client/serveur")
    print("  Lancement : http://127.0.0.1:8000")
    print("  (Le serveur whisper-live doit être joignable sur le LAN — voir")
    print("   la section « Serveur » du panneau web.)")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000)
