"""Seuils des filtres de robustesse (ajout du 2026-11-10).

PROVISOIRES : ils ne sont pas calibrés. `walkforward.py` mesure si chaque filtre prédit le résultat des 30 jours
suivants ; ne modifier un seuil qu'après cette mesure, jamais pour faire passer plus de wallets.
"""
DAY_MS = 86_400_000

# Rendements journaliers (PnL réalisé net de frais, par jour UTC, sur la fenêtre des fills = 90 j)
MIN_ACTIVE_DAYS = 20
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_LOWER_QUANTILE = 0.05
BOOTSTRAP_SEED = 20261110

# Régularité : part de semaines positives parmi les semaines actives (12 dernières semaines)
WEEKS_LOOKBACK = 12
MIN_ACTIVE_WEEKS = 4
MIN_POSITIVE_WEEK_SHARE = 0.5

# Alpha contre beta : jugé seulement avec assez de réductions suivies et de jours baissiers actifs
MIN_BENCH_REDUCTIONS = 10
MIN_DOWN_DAYS = 10
BENCH_COIN = "BTC"
BENCH_DAYS = 91

# Statistiques trop belles pour être vraies (petit échantillon, grille, pertes gardées ouvertes)
MAX_PLAUSIBLE_PF = 10.0

# Règles de sortie d'un wallet suivi
EXIT_MIN_AV = 20_000.0
EXIT_MAX_DD30 = 0.20
EXIT_MAX_LATENT_LOSS = 0.15
EXIT_MAX_INACTIVE_DAYS = 7.0
EXIT_MAX_AVG_LEVERAGE = 8.0
EXIT_DAY_DROP = -0.30

# Conservation des données pour la validation walk-forward
KEEP_FILLS_DAYS_AFTER_PASS = 45
HISTORY_KEEP_DAYS = 180
