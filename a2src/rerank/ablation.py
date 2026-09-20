"""Q3.2-Q3.4: improve on the NRMS-lite baseline, ablate the improvement,
and ship a paired bootstrap 95% CI -- using A1's ``paired_difference_ci``
directly rather than reimplementing the statistic (as the teammate's
notebook did, correctly, but from scratch).

The "one principled improvement" (Q3.2) IS Q2's re-ranker: NRMS-lite only
ever sees article content (embeddings of history and candidates); the
LightGBM re-ranker adds Q1's behavioural features (recency decay, session
context, freshness, category match, popularity) on top of retrieval
signal. That is a legitimate, nameable improvement in the brief's own
example list ("category-aware features, freshness weighting").

The ablation isolates a SPECIFIC feature family's contribution by retraining
the re-ranker with it excluded (``reranker.train_reranker``'s
``exclude_features``) and comparing per-impression nDCG@10 against the full
model -- not against NRMS-lite, which differs in more ways than one feature
family and so cannot isolate anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

_A1_ROOT = Path(__file__).resolve().parents[2] / "Assignment-1-Lexical-Semantic-Retrieval"
if str(_A1_ROOT) not in sys.path:
    sys.path.insert(0, str(_A1_ROOT))

from src.eval.bootstrap import paired_difference_ci  # noqa: E402
from src.eval.metrics import auc, mrr, ndcg_at_k  # noqa: E402

from .features import CandidateFeatures  # noqa: E402
from .reranker import score_reranker, train_reranker  # noqa: E402


def per_impression_metrics(
    rows: list[CandidateFeatures], scores: dict[tuple[str, str], float]
) -> dict[str, list[float]]:
    """Group scored candidate rows by impression, rank each impression's
    slate by score, and compute the slate-regime metrics per impression --
    the array shape ``paired_difference_ci`` and A1's ``bootstrap_ci`` both
    require (per-impression, never pre-averaged; see eval/metrics.py's
    module docstring on why)."""
    by_imp: dict[str, list[CandidateFeatures]] = {}
    for r in rows:
        by_imp.setdefault(r.impression_id, []).append(r)

    out = {"auc": [], "mrr": [], "ndcg5": [], "ndcg10": [], "impression_id": []}
    for imp_id, group in by_imp.items():
        clicked = {r.article_id for r in group if r.clicked}
        if not clicked:
            continue
        ranked = sorted(group, key=lambda r: -scores.get((r.impression_id, r.article_id), float("-inf")))
        ranked_ids = [r.article_id for r in ranked]
        a = auc(ranked_ids, clicked)
        if a is None:
            continue
        out["auc"].append(a)
        out["mrr"].append(mrr(ranked_ids, clicked))
        out["ndcg5"].append(ndcg_at_k(ranked_ids, clicked, 5))
        out["ndcg10"].append(ndcg_at_k(ranked_ids, clicked, 10))
        out["impression_id"].append(imp_id)
    return out


def run_ablation(
    train_rows: list[CandidateFeatures],
    val_rows: list[CandidateFeatures],
    exclude_features: list[str],
    metric: str = "ndcg10",
    num_boost_round: int = 200,
) -> dict:
    """Train the full re-ranker and an ablated variant on the SAME train
    rows, score both on the SAME val rows, and return the paired-bootstrap
    comparison on ``metric``.

    Both models see the same impressions in the same order by construction
    (both are scored from ``val_rows``), which is the precondition
    ``paired_difference_ci``'s docstring states for the comparison to be
    valid -- not asserted again here because it cannot fail given how the
    two score dicts are built from one shared row list.
    """
    full_booster, full_features = train_reranker(train_rows, num_boost_round=num_boost_round)
    ablated_booster, ablated_features = train_reranker(
        train_rows, num_boost_round=num_boost_round, exclude_features=exclude_features
    )

    full_scores = score_reranker(full_booster, full_features, val_rows)
    ablated_scores = score_reranker(ablated_booster, ablated_features, val_rows)

    full_metrics = per_impression_metrics(val_rows, full_scores)
    ablated_metrics = per_impression_metrics(val_rows, ablated_scores)

    # Align by impression id -- the two metric dicts may have dropped
    # different impressions (e.g. AUC undefined for an all-positive slate can
    # differ if scores tie differently), so index-position alignment alone is
    # not safe; intersect explicitly.
    common = set(full_metrics["impression_id"]) & set(ablated_metrics["impression_id"])
    full_idx = {iid: i for i, iid in enumerate(full_metrics["impression_id"])}
    abl_idx = {iid: i for i, iid in enumerate(ablated_metrics["impression_id"])}

    a_vals = [full_metrics[metric][full_idx[iid]] for iid in common]
    b_vals = [ablated_metrics[metric][abl_idx[iid]] for iid in common]

    mean_diff, lo, hi, significant = paired_difference_ci(a_vals, b_vals)
    return {
        "metric": metric,
        "excluded_features": exclude_features,
        "n_paired_impressions": len(common),
        "full_mean": sum(a_vals) / len(a_vals) if a_vals else float("nan"),
        "ablated_mean": sum(b_vals) / len(b_vals) if b_vals else float("nan"),
        "mean_diff": mean_diff,
        "ci_low": lo,
        "ci_high": hi,
        "significant": significant,
    }
