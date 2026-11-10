# Améliorations de la sélection — 10 novembre 2026

Correctif préparé à partir de l'export `hypertracker_strategie_20261110_0007.zip`.
**Rien n'est déployé** : le projet réel (`D:\bot gpt\hypertracker`) et la box ne sont pas accessibles depuis cette
machine. Tout reste en paper ; aucune politique `policies_multi/*.json` n'est modifiée (leurs empreintes sont intactes).

## Contenu

| Dossier | À copier vers | Fichiers |
|---|---|---|
| `box/hl-scan_daemon/` | `/workspace/hl-scan/daemon/` sur la box | `scanner.py`, `lib/score.py` modifiés ; `lib/{criteria,daily,alpha,robust,bench,history,exits,book}.py`, `walkforward.py`, `hip3.disabled`, `tests/` nouveaux |
| `pc/hypertracker/` | `D:\bot gpt\hypertracker\` | `manual_copy_engine.py`, `manual_copy_multi_runner.py`, `add_wallet.py`, `multi_runner.json`, `tools/pick_placebos.py` modifiés ; `tools/{book_data,probation_report,stop_analysis,export_book,apply_exits}.py`, `tests/test_*_1110.py` nouveaux |

`lib/metrics.py`, `lib/sim.py`, `lib/net.py` et `tools/val_common.py` sont des copies inchangées.

## Installation

**PC**
1. `powershell -ExecutionPolicy Bypass -File stop_multi2.ps1`
2. Sauvegarder les cinq fichiers modifiés, puis copier `pc/hypertracker/` par-dessus le projet.
   Attention : `multi_runner.json` vient de l'export du 10/11 ; si tu as ajouté des wallets depuis, reporte à la main
   les trois changements (voir « Configuration ») au lieu d'écraser le fichier.
3. `py -3 manual_copy_multi_runner.py --check` : chaque compte doit afficher `checkpoint_ok`.
4. `py -3 -m pytest tests/test_improvements_1110.py tests/test_tools_1110.py tests/test_manual_copy.py tests/test_validation.py`
5. `powershell -ExecutionPolicy Bypass -File start_multi2.ps1`

**Box**
1. `./stop.sh`, sauvegarder `scanner.py` et `lib/score.py`, copier `box/hl-scan_daemon/`.
2. Sur le PC : `py -3 tools\export_book.py`, puis copier `book.json` dans le dossier du scanner.
3. `python3 -m pytest tests -q`, puis `./start.sh`.

## Ce qui change

| # | Amélioration | Mise en œuvre |
|---|---|---|
| 1 | Alpha, pas beta | `lib/alpha.py` : PnL long/short séparés, témoin BTC (mêmes instants, même notionnel, même sens), PnL les jours où BTC baisse. Échec `BETA_ONLY` si le wallet ne bat pas le témoin **et** perd les jours de baisse. |
| 2 | Walk-forward | `walkforward.py --mode fills` (dès maintenant, univers biaisé) et `--mode history` (sans biais, après 30 jours d'historique). |
| 3 | Historique des analyses | Table `analyses` en ajout seul dans `state.db` ; les fills d'un wallet sont gardés 45 jours après son dernier passage. |
| 4 | Rendements journaliers | `lib/daily.py` : au moins 20 jours actifs et borne basse (5 %) de la moyenne journalière au-dessus de zéro. PF > 10 devient bloquant. |
| 5 | Fin du filtre « 7 jours > 0 » | Remplacé par au moins 50 % de semaines positives sur 12 semaines ; 7 jours négatifs = simple drapeau. |
| 6 | Essai fantôme | `add_wallet.py` crée le book en `mode: probation` (seul le fantôme trade). `tools\probation_report.py` applique la règle de promotion fixée. `--live` pour s'en passer. |
| 7 | Règles de sortie | Le scanner surveille tous les wallets de `book.json` et écrit `book_exits.json` ; `tools\apply_exits.py` passe les books concernés en `mode: retired` (sorties seules). |
| 8 | Corrélation | `max_books_same_coin_side: 5` : pas de nouvelle entrée tant que 5 autres books tiennent le même coin dans le même sens. |
| 9 | Placebos | **Non augmentés**, voir « Limites ». `pick_placebos.py` utilise maintenant le même âge minimal que le scanner (30 jours). |
| 10 | HIP-3 coupé | `allow_hip3: false` sur les 13 books (sorties seules pour les copies ouvertes) et fichier `hip3.disabled` côté scanner. |
| 11 | Analyse des stops | `tools\stop_analysis.py` : prix du stop comparé à la sortie ultérieure du leader. |

## Configuration (`multi_runner.json`)

- `allow_hip3` : `true` → `false` pour 0f4f, 0fe2, 1609, 2215, 3de9, 4270, 79bf, 7ffb, a2e3, ac14, d238, f36f, f838.
- `max_books_same_coin_side: 5` et `max_portfolio_coin_side_notional: null` (nouveaux).
- `books.<préfixe>.mode` : `probation` ou `retired` (absent = `live`).

## Commandes utiles

```powershell
py -3 tools\apply_exits.py --state hypertracker_state            # essai à blanc ; ajouter --write pour appliquer
py -3 tools\apply_exits.py --state hypertracker_state --exits book_exits.json --write
py -3 tools\probation_report.py --state hypertracker_state --config multi_runner.json
py -3 tools\stop_analysis.py --state hypertracker_state
```

```bash
python3 walkforward.py --mode fills      # sur la box
python3 walkforward.py --mode history    # utile après 30 jours d'historique
```

## Limites à connaître

- **Seuils non calibrés.** 20 jours actifs, 50 % de semaines positives, 5 books par coin : valeurs provisoires dans
  `lib/criteria.py` et `multi_runner.json`. `walkforward.py` sert à les confirmer ou à les retirer.
- **Moins de wallets passeront.** Les nouveaux filtres sont plus stricts ; l'effet réel n'a pas pu être mesuré ici
  (pas de fills réels dans l'export).
- **Plus de requêtes côté scanner.** Sans le rejet « 7 jours ≤ 0 » au préfiltre, davantage de wallets ont leurs fills
  téléchargés. Le budget de 600 de poids par minute de `net.py` reste la limite ; le scan sera simplement plus lent.
- **Placebos.** Le poids REST estimé du runner est déjà de 745/min pour un budget de 750 ; chaque placebo coûte
  environ 7,5/min. Pour en ajouter : désactiver (`enabled: false`) les books retirés une fois à plat, ou passer des
  wallets temps réel en différé (36 → 7,5 de poids chacun), puis monter `max_placebos` (maximum 20).
- **Témoin BTC.** Il mesure le choix des actifs, pas le timing : un trader qui ne traite que BTC a un excédent nul.
  C'est pourquoi `BETA_ONLY` exige aussi une perte les jours de baisse.
  Si les bougies BTC sont indisponibles, l'analyse du wallet est reportée (erreur, nouvel essai 6 h plus tard, statut
  précédent conservé) : une donnée manquante ne devient jamais un verdict favorable.
- **Plafond de corrélation et redémarrage.** Le plafond est branché après la relecture du journal au démarrage : les
  quelques secondes d'observations rejouées ne sont pas plafonnées. Il limite aussi les renforcements, pas seulement
  les ouvertures.
- **`book_exits.json`.** Un wallet dont la vérification échoue n'y figure pas ce tour-là. `apply_exits.py` refuse un
  fichier de plus de 12 heures et ne réactive jamais un book retiré.
- **`book.json`.** Lu au démarrage du scanner : le recopier puis relancer le scanner après chaque ajout de wallet.
- **Tests.** 80 tests scanner et 196 tests PC passent sur données synthétiques ; `scanner.py` et le runner ont été
  vérifiés par un essai d'intégration sans réseau, et le correctif a été relu par un agent de revue Python.
  Aucun essai contre l'API réelle ni sur les machines de production.
