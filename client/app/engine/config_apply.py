# -*- coding: utf-8 -*-
"""
app/engine/config_apply.py
==========================
Planification du changement de configuration : quels champs imposent un
redémarrage du moteur (RELOAD_FIELDS), quels champs sont applicables
« à chaud » (HOT_FIELDS).

Adapté de l'original (ref/app/engine/config_apply.py) au modèle client/
serveur de Talky (roadmap §5.7) :
  * RELOAD_FIELDS ne contient plus que {audio_device} (pas de modèle local
    ni de GPU à recharger) ;
  * live_changed retourne la LISTE des champs HOT_FIELDS modifiés.
"""

from app.core.constants import HOT_FIELDS, RELOAD_FIELDS


def plan_changes(old: dict, new: dict) -> tuple[bool, list[str]]:
    """
    Compare deux configurations.

    :return: (reload_needed, live_changed)
      * reload_needed : un redémarrage complet (périphérique audio) est requis ;
      * live_changed   : liste des champs HOT_FIELDS modifiés, applicables
        sans redémarrer (touche, langue, modèle, VAD, injection, serveur...).
    """
    reload_needed = any(
        new.get(k, old.get(k)) != old.get(k) for k in RELOAD_FIELDS
    )
    live_changed = [
        k for k in HOT_FIELDS
        if new.get(k, old.get(k)) != old.get(k)
    ]
    return reload_needed, live_changed
