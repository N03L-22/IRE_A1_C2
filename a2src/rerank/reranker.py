"""Q2.2/Q2.3: the GBDT re-ranker (LightGBM option A), trained over Q1's
candidate-feature table and scored back onto the same candidates.

LightGBM's ``lambdarank`` objective is used rather than plain binary
classification: Q2 asks for a RE-RANKER, and lambdarank optimises pairwise
ordering within a group (here, one impression's candidates) directly, which
is what nDCG/MRR actually reward -- a classifier trained on
click-vs-no-click independently of grouping would optimise a different,
looser objective.
"""

from __future__ import annotations

from dataclasses import dataclass

import lightgbm as lgb
import numpy as np

from .features import FIELDS, CandidateFeatures

DEFAULT_PARAMS = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "ndcg_eval_at": [5, 10],
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 20,
    "verbose": -1,
}


@dataclass
class RerankerData:
    """A candidate table, split into what LightGBM's ranker API wants:
    a feature matrix, labels, and per-group sizes ORDERED so all rows of one
    impression are contiguous -- lambdarank groups by contiguous run length,
    not by an explicit group id column."""

    X: np.ndarray
    y: np.ndarray
    group_sizes: list[int]
    feature_names: list[str]
    row_impression_ids: list[str]
    row_article_ids: list[str]


def to_training_data(
    rows: list[CandidateFeatures],
    exclude_features: list[str] | None = None,
    extra_features: list[str] | None = None,
) -> RerankerData:
    """``extra_features`` adds fields NOT in the default ``FIELDS`` list --
    the one case is ``display_position``, which ``features.py`` excludes by
    default because it leaks under corpus-wide retrieve-then-rank
    (``candidates.build_rerank_table``), but is a genuine, safe Q1.2
    position-bias signal when training rows come from
    ``candidates.build_rerank_table_on_slate`` instead (every candidate is a
    real slate position there, no retrieval simulation, no append-on-miss,
    so nothing about its presence correlates with being the click). Callers
    training on slate-based rows may opt in explicitly; callers training on
    ``build_rerank_table`` rows must not, and passing it there is the
    caller's mistake, not a corruption this function can detect from the
    rows alone (it doesn't know which builder produced them)."""
    exclude = set(exclude_features or [])
    feature_names = [f for f in FIELDS if f not in exclude]
    for f in extra_features or []:
        if f not in feature_names:
            feature_names.append(f)

    by_impression: dict[str, list[CandidateFeatures]] = {}
    for r in rows:
        by_impression.setdefault(r.impression_id, []).append(r)

    X_rows, y_rows, group_sizes = [], [], []
    row_imp_ids, row_art_ids = [], []
    for imp_id, group in by_impression.items():
        group_sizes.append(len(group))
        for r in group:
            X_rows.append([_as_float(getattr(r, f)) for f in feature_names])
            y_rows.append(r.clicked)
            row_imp_ids.append(imp_id)
            row_art_ids.append(r.article_id)

    return RerankerData(
        X=np.asarray(X_rows, dtype=np.float64),
        y=np.asarray(y_rows, dtype=np.int32),
        group_sizes=group_sizes,
        feature_names=feature_names,
        row_impression_ids=row_imp_ids,
        row_article_ids=row_art_ids,
    )


def _as_float(v) -> float:
    """freshness_hours is Optional (None on MIND, which has no
    published_time) -- LightGBM handles NaN natively as "missing", which is
    the honest representation rather than a magic sentinel like -1."""
    return float("nan") if v is None else float(v)


def train_reranker(
    rows: list[CandidateFeatures],
    num_boost_round: int = 200,
    exclude_features: list[str] | None = None,
    extra_features: list[str] | None = None,
    params: dict | None = None,
) -> tuple[lgb.Booster, list[str]]:
    """Train the re-ranker. Returns ``(booster, feature_names)`` -- the
    feature order used at train time, which ``score_reranker`` must match
    exactly since LightGBM has no column-name binding at predict time.

    ``extra_features`` -- see ``to_training_data``'s docstring for the one
    intended use (``display_position`` on slate-trained rows only)."""
    data = to_training_data(rows, exclude_features, extra_features)
    if sum(data.group_sizes) == 0 or not any(data.y):
        raise ValueError(
            "no positive (clicked) rows in the training table -- check that "
            "candidate retrieval actually returns the clicked article "
            "(see candidates.retrieve_candidates)"
        )
    ds = lgb.Dataset(data.X, label=data.y, group=data.group_sizes, feature_name=data.feature_names)
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    booster = lgb.train(p, ds, num_boost_round=num_boost_round)
    return booster, data.feature_names


def score_reranker(
    booster: lgb.Booster, feature_names: list[str], rows: list[CandidateFeatures]
) -> dict[tuple[str, str], float]:
    """Score every (impression_id, article_id) candidate row.

    Returns a lookup rather than mutating ``rows`` in place -- keeps the
    feature table immutable so the same table can be scored by several
    model checkpoints (full vs. ablated) without rebuilding it.
    """
    X = np.asarray([[_as_float(getattr(r, f)) for f in feature_names] for r in rows], dtype=np.float64)
    preds = booster.predict(X)
    return {(r.impression_id, r.article_id): float(s) for r, s in zip(rows, preds)}


def feature_importance(booster: lgb.Booster, feature_names: list[str]) -> list[tuple[str, float]]:
    """Gain-based importance, normalised to sum 1 -- for the design note's
    "what did the re-ranker actually use" table."""
    gains = booster.feature_importance(importance_type="gain")
    total = gains.sum() or 1.0
    pairs = sorted(zip(feature_names, gains / total), key=lambda kv: -kv[1])
    return [(name, float(g)) for name, g in pairs]
