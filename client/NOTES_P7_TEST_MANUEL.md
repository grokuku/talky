# P7 → P8 : Note de test manuel du frontend

Frontend livré en P7 (à valider manuellement en P8, sur CachyOS + serveur LAN réel) :

| Fichier | Rôle |
| :--- | :--- |
| `client/templates/index.html` | page unique (SPA légère, sans framework) |
| `client/static/style.css` | design « verre » sur fond `#141821` (aucune dépendance externe) |
| `client/static/app.js` | logique WS + polling REST + section Serveur + capture raccourci |

Lancer : `python main.py` → ouvrir `http://127.0.0.1:8000` (depuis le poste CachyOS
ou un téléphone du LAN via `http://192.168.x.x:8000` si le serveur écoute sur le LAN).

---

## 1. Chargement & affichage

- [ ] La page s'affiche avec le design « verre » (fond `#141821`, halos colorés, panneaux translucides).
- [ ] Aucun appel réseau externe (pas de CDN) : vérifier dans les DevTools (onglet Network) que seules
      des requêtes vers `127.0.0.1:8000` / `192.168.x.x:8000` sont émises.
- [ ] Au chargement : le formulaire est pré-rempli avec `config.json` (URL serveur, modèle, langue…),
      l'historique s'affiche (ou « Aucune transcription pour l'instant. »), le badge serveur passe à
      « Connecté / Déconnecté » selon l'état réel.
- [ ] Le sélecteur de micro (`/api/devices/audio`) est peuplé avec les entrées du système.

## 2. WebSocket & temps réel

- [ ] Badge « Temps réel » visible dans l'en-tête de l'historique quand la WS est connectée.
- [ ] Lancer le moteur → le badge moteur passe `Démarrage…` (sky) puis `En attente` (mint), la barre
      d'état affiche modèle / cuda / int8 / raccourci.
- [ ] Appuyer sur le raccourci : `Enregistrement…` (rose, pulsant) puis **« Transcription serveur… »** (sky)
      puis `Succès` — le texte apparaît en haut de l'historique **en temps réel** (heure · durée · langue).
- [ ] Couper la WS (arrêter/relancer `main.py`) → repli polling REST : la page continue de se rafraîchir
      (badge « Temps réel » masqué), puis reconnexion auto (backoff 3 s → 15 s) et reprise du temps réel.

## 3. Section « Serveur »

- [ ] Badge « Serveur connecté » (vert) quand le serveur speaches répond, « Serveur déconnecté » (rouge)
      sinon — mis à jour toutes les 5 s sans recharger la page.
- [ ] Renseigner `server_url` + `server_api_key` (champ masqué) + `server_timeout`, cliquer
      « Tester la connexion » : résultat affiché (joignable + latence en ms + liste des modèles).
- [ ] Cas erreur : URL invalide ou serveur coupé → panneau rouge « Serveur injoignable » + message explicite.
- [ ] Cas 401/403 : clé API vide alors que le serveur exige une clé → erreur claire (R12).
- [ ] `Accélération` / `Compute type` affichés en lecture seule (`cuda` / `int8`).
- [ ] Le champ modèle propose en datalist les modèles listés par `/api/server/status` + presets usuels,
      et reste une saisie libre (ex. repo Hugging Face).

## 4. Formulaire de configuration

- [ ] Modifier modèle/langue/tâche/VAD/micro/max_history puis « Enregistrer les paramètres » :
      feedback vert « appliqués à chaud » (avec liste `live_changed`) ou peach « redémarrage requis »
      (cas `audio_device` → le moteur redémarre).
- [ ] Erreur de validation (ex. `server_url` vide, `server_timeout < 5`) : feedback rouge avec le détail champ par champ.
- [ ] Mode de saisie + cases d'injection appliqués immédiatement à chaud (sans cliquer Enregistrer) + toast.
- [ ] Pendant le polling REST, la saisie en cours dans un champ n'est pas écrasée (garde `activeElement`).

## 5. Capture de raccourci

- [ ] Cliquer sur la touche → elle pulse en bleu ; appuyer `F8` → affiche `f8` et l'enregistre à chaud.
- [ ] Combinaisons : `Ctrl+Espace` → `ctrl+space`, `Ctrl+Alt+F9` → `ctrl+alt+f9`, `Meta` → `super`.
- [ ] `Échap` annule la capture sans modifier la valeur.
- [ ] Un modificateur seul (Ctrl/Alt/Shift/Meta) ne termine pas la capture (on attend la touche principale).
- [ ] Le raccourci enregistré est bien celui détecté par `parse_hotkey` (format compatible evdev, lettres minuscules).

## 6. Contrôle du moteur

- [ ] Interrupteur ON/OFF : démarre (`Démarrage…` → `En attente`) / arrête (`Arrêt…` → `Hors ligne`).
- [ ] Bouton « Redémarrer » : cycle complet stop → start.
- [ ] Dictée complète push-to-talk : hotkey maintenue → texte injecté dans la fenêtre active (Ctrl+V).
- [ ] Mode toggle : un appui démarre l'enregistrement, un second le termine.

## 7. Historique

- [ ] Les transcriptions s'affichent les plus récentes en premier, avec heure · durée · langue.
- [ ] « Copier » copie le texte dans le presse-papier (avec repli si contexte non sécurisé).
- [ ] « Vider » supprime l'historique (DELETE /api/history) et vide la liste.

## 8. Responsive & mobile

- [ ] Réduire la fenêtre < 1024 px : grille sur une colonne, header qui s'enroule, liste limitée en hauteur.
- [ ] Depuis un téléphone sur le LAN : la page est utilisable (touch targets, toast visible).

## 9. Accessibilité

- [ ] Navigation clavier : focus visible (anneau sky) sur tous les éléments interactifs.
- [ ] `prefers-reduced-motion: reduce` : animations désactivées.
- [ ] Labels associés aux champs ; `aria-live` sur le feedback et l'historique.

## 10. Erreurs réseau

- [ ] Serveur speaches coupé pendant la dictée → statut `Erreur` (rouge) avec message FR
      (« Serveur injoignable — vérifier server_url », etc.) puis retour à `En attente` sans retry automatique.
- [ ] `main.py` coupé pendant que la page est ouverte → erreurs propres dans la console, pas de crash JS.

---

### Critères P7 « done » (rappel roadmap §9)

- [ ] `node --check static/app.js` OK.
- [ ] Panneau sur `127.0.0.1:8000` (GET / → index.html, /static monté).
- [ ] Badge WS (temps réel) fonctionnel.
- [ ] Section Serveur fonctionnelle (badge 5 s + « Tester la connexion »).
- [ ] Capture raccourci `ev.code`/`ev.key` appliquée à chaud.
- [ ] Historique live (WS + polling REST de repli).
