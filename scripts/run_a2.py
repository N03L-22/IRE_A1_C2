"""One-command A2 pipeline: Q1-Q5, one dataset per invocation (Q1.5's
"one-command rebuild" requirement, mirroring A1's `make eval` / `src.eval.run`
pattern -- see that module's docstring).

    .venv/bin/python -m scripts.run_a2 --dataset ebnerd --limit 4000 \
        --out results/a2_ebnerd.json
    .venv/bin/python -m scripts.run_a2 --dataset mind --limit 4000 \
        --candidate-generator bm25 --out results/a2_mind_bm25only.json

Runs, in order: Q2 candidate table (default: BM25+Semantic fused by A1's
RRFusion -- "Assignment 1's candidate generator" in full, not just its
lexical half; ``--candidate-generator bm25`` reverts to BM25-only for
comparison) -> Q2 LightGBM re-ranker -> Q3 NRMS-lite baseline + ablation
with paired bootstrap CI -> Q4 serving benchmark -> Q5 extended eval
through A1's harness. Everything is printed and written to ``--out`` as one
JSON report.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

_A2_ROOT = Path(__file__).resolve().parents[1]
_A1_ROOT = _A2_ROOT.parent / "Assignment-1-Lexical-Semantic-Retrieval"
sys.path.insert(0, str(_A2_ROOT))
sys.path.insert(0, str(_A1_ROOT))

from src.data.readers import SPLIT_NAMES, get_reader  # noqa: E402
from src.eval.harness import evaluate, to_dicts  # noqa: E402
from src.resources import InsufficientResources, add_arguments, from_args  # noqa: E402
from src.retrieval.bm25 import BM25Retriever  # noqa: E402
from src.retrieval.fusion import RRFusion  # noqa: E402
from src.retrieval.semantic import SemanticRetriever  # noqa: E402

from a2src.rerank.ablation import run_ablation  # noqa: E402
from a2src.rerank.candidates import (  # noqa: E402
    DEFAULT_TOP_K,
    RetrievalCoverage,
    UnionRetriever,
    build_rerank_table,
    retrieve_candidates,
    train_popularity_from_impressions,
)
from a2src.rerank.features import build_candidate_rows  # noqa: E402
from a2src.rerank.nrms_lite import NRMSLite, build_training_examples, score_impression, train_nrms_lite  # noqa: E402
from a2src.rerank.reranked_retriever import RerankedRetriever  # noqa: E402
from a2src.rerank.reranker import feature_importance, score_reranker, train_reranker  # noqa: E402
from a2src.serving.bench import cost_per_1000_queries, measure_build_memory, measure_latency  # noqa: E402
from src.eval.metrics import auc as auc_metric, mrr as mrr_metric, ndcg_at_k  # noqa: E402
from src.retrieval.encode import encode_cached  # noqa: E402

log = logging.getLogger("run_a2")

#: A1's Phase-2-sweep-chosen BM25 params per dataset (F23; src/eval/run.py).
BM25_PARAMS = {
    "ebnerd": dict(k1=1.6, b=1.0, last_n=15),
    "mind": dict(k1=1.6, b=0.75, last_n=5),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", choices=["mind", "ebnerd"], required=True)
    p.add_argument("--tier", default="small")
    p.add_argument("--work-dir", type=Path, default=_A1_ROOT / "data" / "work")
    p.add_argument("--embed-cache-dir", type=Path, default=_A1_ROOT / "data" / "store" / "embeddings")
    p.add_argument("--limit", type=int, default=4000, help="impressions to build the rerank train table from")
    p.add_argument("--val-limit", type=int, default=1000)
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--num-boost-round", type=int, default=200)
    p.add_argument("--candidate-generator", choices=["bm25", "bm25+semantic", "bm25+semantic+union"], default="bm25+semantic")
    p.add_argument("--seeds", type=int, nargs="+", default=None,
                    help="if given, repeat re-ranker training with each seed and report mean+-sd "
                         "AUC/nDCG instead of a single run (Q notes: seed-robustness check)")
    p.add_argument("--feature-availability-check", action="store_true",
                    help="also train a variant excluding click-log-derived features "
                         "(recency_weighted_click_count, history_click_count, session_click_count) "
                         "to quantify serving-time degradation if those features go stale/missing")
    p.add_argument("--out", type=Path)
    add_arguments(p)  # A1's --n-jobs/--mem-gb/--ignore-availability (src/resources.py)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("bm25s").setLevel(logging.ERROR)

    # Refuse to start on a machine that is already tight on memory, rather
    # than launching a multi-GB candidate-table build into a system running
    # on swap. This pipeline had NO memory guard until a manual investigation
    # script was launched with only 322MB free RAM and 5.6GB already in swap
    # (a concurrent full-scale submission job was mid-run) -- A1's own
    # resources.py exists for exactly this and was simply never wired in here.
    try:
        budget = from_args(args)
    except InsufficientResources as exc:
        log.error("%s", exc)
        return 3
    log.info("%s", budget)

    dataset = args.dataset
    reader = get_reader(dataset, args.work_dir, args.tier)
    split = SPLIT_NAMES[dataset]["train"]
    heldout_split = SPLIT_NAMES[dataset]["heldout"]

    log.info("=== %s: loading ===", dataset)
    articles = {a.article_id: a for a in reader.articles()}
    histories = {h.user_id: h for h in reader.histories(split)}

    train_imps = []
    for imp in reader.impressions(split):
        train_imps.append(imp)
        if len(train_imps) >= args.limit * 2:  # room for the train/val split below
            break
    val_imps = []
    for imp in reader.impressions(heldout_split):
        val_imps.append(imp)
        if len(val_imps) >= args.val_limit:
            break
    log.info("  %d train-period impressions, %d heldout impressions, %d articles",
              len(train_imps), len(val_imps), len(articles))

    train_popularity = train_popularity_from_impressions(train_imps)

    # --- candidate generation (Q2.1) ---
    log.info("=== %s: Q2 candidate generation + re-ranker (%s) ===", dataset, args.candidate_generator)
    bm25_params = BM25_PARAMS[dataset]
    (candidate_gen, gen_mem_mb) = measure_build_memory(
        lambda: _build_candidate_generator(
            list(articles.values()), bm25_params, args.candidate_generator, args.embed_cache_dir
        )
    )
    log.info("  candidate generator (%s) index: %.1f MB for %d articles (bm25_params=%s)",
              candidate_gen.name, gen_mem_mb, len(articles), bm25_params)

    # Q2.1's recall@K ceiling: measured BEFORE the guaranteed append rescues
    # a missed click, over train+val together (RetrievalCoverage's docstring
    # -- a single shared instance accumulates across both calls).
    coverage = RetrievalCoverage()
    train_rows = build_rerank_table(
        candidate_gen, train_imps, histories, articles, train_popularity,
        top_k=args.top_k, max_impressions=args.limit, coverage=coverage,
    )
    val_rows = build_rerank_table(
        candidate_gen, val_imps, histories, articles, train_popularity,
        top_k=args.top_k, max_impressions=args.val_limit, coverage=coverage,
    )
    log.info("  train_rows=%d val_rows=%d", len(train_rows), len(val_rows))
    log.info("  recall@%d: %.4f  precision@%d: %.6f (%d/%d impressions with usable history)",
              args.top_k, coverage.recall_at_k, args.top_k, coverage.precision_at_k,
              coverage.impressions_with_click_retrieved, coverage.impressions_checked)

    if args.seeds:
        seed_result = _seed_repetition(train_rows, val_rows, args.seeds, args.num_boost_round)
        log.info("  seed repetition (%s): %s", args.seeds, seed_result)
        booster, feature_names = train_reranker(train_rows, num_boost_round=args.num_boost_round)
    else:
        seed_result = None
        booster, feature_names = train_reranker(train_rows, num_boost_round=args.num_boost_round)
    val_scores = score_reranker(booster, feature_names, val_rows)
    importances = feature_importance(booster, feature_names)
    log.info("  feature importance: %s", importances)

    feature_availability_result = None
    if args.feature_availability_check:
        # Serving-time question: if the click-log-derived features go stale
        # or unavailable (e.g. a cold cache, a user with no logged session),
        # how much AUC/nDCG does the re-ranker actually lose? Reuses the
        # existing single-family ablation machinery (run_ablation) with the
        # full click-log feature set excluded together, rather than one at a
        # time -- this pipeline had never quantified that degradation as a
        # single number before.
        feature_availability_result = run_ablation(
            train_rows, val_rows,
            exclude_features=[
                "recency_weighted_click_count", "history_click_count", "session_click_count",
            ],
            num_boost_round=args.num_boost_round,
        )
        log.info("  feature availability (drop all click-log features): %s", feature_availability_result)

    before_after = _before_after_eval(val_imps, histories, articles, candidate_gen, val_rows, val_scores, args.top_k)
    log.info("  before (base retriever score) vs after (re-ranker): %s", before_after)

    # --- Q3: NRMS-lite baseline + ablation ---
    log.info("=== %s: Q3 NRMS-lite baseline + ablation ===", dataset)
    ids = list(articles.keys())
    texts = [articles[a].retrieval_text for a in ids]
    vecs, _ = encode_cached(texts, ids, cache_dir=str(args.embed_cache_dir))
    article_vecs = dict(zip(ids, vecs))

    examples = build_training_examples(train_imps, histories, article_vecs, max_examples=args.limit)
    nrms_result = None
    if examples:
        model = NRMSLite(emb_dim=vecs.shape[1])
        losses = train_nrms_lite(model, examples, epochs=3)
        nrms_ndcg10 = []
        for imp in val_imps:
            hist = histories.get(imp.user_id)
            if hist is None:
                continue
            past = hist.before(imp.time)
            scores = score_impression(model, imp, past, article_vecs)
            if not scores:
                continue
            ranked = sorted(scores, key=lambda aid: -scores[aid])
            clicked = set(imp.clicked)
            if clicked & set(ranked):
                nrms_ndcg10.append(ndcg_at_k(ranked, clicked, 10))
        nrms_result = {
            "epoch_losses": losses,
            "n_train_examples": len(examples),
            "val_ndcg10_mean": sum(nrms_ndcg10) / len(nrms_ndcg10) if nrms_ndcg10 else None,
            "n_val_scored": len(nrms_ndcg10),
        }
        log.info("  NRMS-lite: %s", nrms_result)
    else:
        log.warning("  no NRMS-lite training examples (no usable history+embeddings) -- skipped")

    ablation_result = run_ablation(
        train_rows, val_rows,
        exclude_features=["recency_weighted_click_count", "history_click_count"],
        num_boost_round=args.num_boost_round,
    )
    log.info("  ablation (drop recency features): %s", ablation_result)

    # --- Q4: serving/scale benchmark ---
    log.info("=== %s: Q4 serving benchmark ===", dataset)
    sample_imps = val_imps[: min(150, len(val_imps))]

    def score_one(imp):
        hist = histories.get(imp.user_id)
        past = hist.before(imp.time) if hist else []
        cand_ids, _appended, retrieval_scores = retrieve_candidates(
            candidate_gen, past, articles, imp, top_k=args.top_k
        )
        synthetic = type(imp)(
            impression_id=imp.impression_id, user_id=imp.user_id, time=imp.time,
            candidates=cand_ids, clicked=imp.clicked, session_id=imp.session_id,
        )
        times_by_id = dict(zip(hist.clicked_ids, hist.times)) if hist and hist.is_verifiable else None
        rows = build_candidate_rows(
            synthetic, past, times_by_id, articles, train_popularity,
            retrieval_score_by_article=retrieval_scores,
        )
        return score_reranker(booster, feature_names, rows)

    latency = measure_latency(score_one, sample_imps, warmup=min(10, len(sample_imps)))
    cost = cost_per_1000_queries(latency["p99_ms"])
    log.info("  latency: %s", latency)
    log.info("  cost/QPS: %s", cost)

    # --- Q5: extended eval through A1's harness ---
    log.info("=== %s: Q5 extended evaluation ===", dataset)
    reranked = RerankedRetriever(candidate_gen, booster, feature_names, articles, train_popularity, top_k=args.top_k)
    reranked.index(list(articles.values()))
    eval_rows = evaluate(
        reranked, val_imps, histories, articles, dataset,
        train_popularity=train_popularity, with_slices=True,
    )
    log.info("  %d result rows (all metrics x slices x CIs)", len(eval_rows))

    report = {
        "dataset": dataset,
        "tier": args.tier,
        "n_articles": len(articles),
        "n_train_impressions": len(train_imps),
        "n_val_impressions": len(val_imps),
        "candidate_generator": args.candidate_generator,
        "bm25_params": bm25_params,
        "candidate_generator_index_memory_mb": gen_mem_mb,
        "rerank_train_rows": len(train_rows),
        "rerank_val_rows": len(val_rows),
        "top_k": args.top_k,
        "recall_at_k": {
            "k": args.top_k,
            "recall": coverage.recall_at_k,
            "precision": coverage.precision_at_k,
            "impressions_with_click_retrieved": coverage.impressions_with_click_retrieved,
            "impressions_checked": coverage.impressions_checked,
        },
        "seed_repetition": seed_result,
        "feature_importance": importances,
        "before_after": before_after,
        "nrms_lite": nrms_result,
        "ablation": ablation_result,
        "feature_availability_check": feature_availability_result,
        "serving_latency_ms": latency,
        "cost_per_1000_queries": cost,
        "q5_extended_eval": to_dicts(eval_rows),
    }

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", args.out)

    return 0


def _build_candidate_generator(articles: list, bm25_params: dict, kind: str, embed_cache_dir: Path):
    """Q2.1's candidate generator: BM25-only, or BM25+Semantic fused by A1's
    RRFusion (the default) -- "Assignment 1's candidate generator" means the
    whole retrieve stack it built, not just its lexical half. Both branches
    satisfy A1's Retriever protocol (``.name``, ``.index``, ``.retrieve``,
    ``.score_subset``), so every downstream caller (``build_rerank_table``,
    ``RerankedRetriever``, ``_before_after_eval``) works unchanged either way.
    """
    bm25 = BM25Retriever(**bm25_params)
    bm25.index(articles)
    if kind == "bm25":
        return bm25
    # SemanticRetriever's own encode_cached call defaults to a CWD-relative
    # "data/store/embeddings", which misses when this script is invoked from
    # Assignment-2-Click-Log-Reranking/ -- A1's cache lives at
    # --embed-cache-dir (Assignment-1.../data/store/embeddings). Encoding
    # here explicitly and passing `vectors=`/`vector_ids=` (the
    # "provided-embeddings baseline" path semantic.py's __init__ already
    # supports) reuses A1's cache correctly instead of a silent, slow
    # re-encode into the wrong directory.
    ids = [a.article_id for a in articles]
    texts = [a.retrieval_text for a in articles]
    vecs, _ = encode_cached(texts, ids, cache_dir=str(embed_cache_dir))
    semantic = SemanticRetriever(vectors=vecs, vector_ids=ids)
    semantic.index(articles)
    if kind == "bm25+semantic+union":
        # Ablation (not the default): SET UNION of BM25's and Semantic's own
        # top-K/2 each, instead of RRFusion's rank fusion below -- see
        # UnionRetriever's docstring for what specifically differs.
        union = UnionRetriever([bm25, semantic])
        union.index(articles)
        return union
    fused = RRFusion([bm25, semantic])
    fused.index(articles)  # idempotent (fusion.py docstring); components already indexed above
    return fused


def _seed_repetition(train_rows, val_rows, seeds: list[int], num_boost_round: int) -> dict:
    """Retrain the SAME config with different LightGBM seeds and report
    mean+-sd val AUC/nDCG@10, instead of reporting one run's number as if it
    were the model's performance.

    Not previously done in this pipeline: every reported number so far has
    been from a single training run, with no sense of how much of the
    train/val gap (or a leaderboard change between attempts) is genuine
    signal versus training-randomness noise. ``train_reranker``'s existing
    ``params`` override (a plain dict merged into LightGBM's params) already
    supports this -- no new plumbing needed, just a loop and aggregation.
    """
    aucs, ndcg10s = [], []
    for seed in seeds:
        booster, feature_names = train_reranker(
            train_rows, num_boost_round=num_boost_round, params={"seed": seed}
        )
        scores = score_reranker(booster, feature_names, val_rows)
        m = per_impression_metrics_local(val_rows, scores)
        if m["auc"]:
            aucs.append(sum(m["auc"]) / len(m["auc"]))
        if m["ndcg10"]:
            ndcg10s.append(sum(m["ndcg10"]) / len(m["ndcg10"]))

    def mean_sd(xs):
        if not xs:
            return {"mean": None, "sd": None}
        mean = sum(xs) / len(xs)
        sd = (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5 if len(xs) > 1 else 0.0
        return {"mean": mean, "sd": sd}

    return {
        "seeds": seeds,
        "auc_per_seed": aucs,
        "ndcg10_per_seed": ndcg10s,
        "auc": mean_sd(aucs),
        "ndcg10": mean_sd(ndcg10s),
    }


def per_impression_metrics_local(rows, scores: dict) -> dict:
    """Same per-impression grouping as ``ablation.per_impression_metrics``,
    duplicated locally rather than imported: that function lives in
    ``a2src.rerank.ablation`` alongside the paired-CI machinery it's built
    for, and pulling it in here for a plain mean+-sd (no pairing, no CI)
    would suggest a coupling this helper doesn't need.
    """
    by_imp: dict[str, list] = {}
    for r in rows:
        by_imp.setdefault(r.impression_id, []).append(r)
    out = {"auc": [], "ndcg10": []}
    for imp_id, group in by_imp.items():
        clicked = {r.article_id for r in group if r.clicked}
        if not clicked:
            continue
        ranked = sorted(group, key=lambda r: -scores.get((r.impression_id, r.article_id), float("-inf")))
        ranked_ids = [r.article_id for r in ranked]
        a = auc_metric(ranked_ids, clicked)
        if a is None:
            continue
        out["auc"].append(a)
        out["ndcg10"].append(ndcg_at_k(ranked_ids, clicked, 10))
    return out


def _before_after_eval(val_imps, histories, articles, base_retriever, val_rows, rerank_scores: dict, top_k: int) -> dict:
    """Q2.4: AUC/MRR/nDCG@5/@10 for the base retriever's OWN score ('before')
    vs. the re-ranker's score ('after'), on the same candidate rows.

    'Before' must come from the base retriever scoring the candidate set
    itself (``score_subset``, the same primitive A1's ``RRFusion`` uses to
    rank a slate without a full corpus retrieval -- fusion.py), NOT from a
    row-order proxy: an earlier version used the candidate list's index,
    which for this pipeline's append-on-miss construction is a near-perfect
    label leak (see ``candidates.retrieve_candidates``'s docstring for the
    full story). Scoring with the retriever's real function makes 'before'
    an honest baseline instead of an artifact of construction order.
    """
    by_imp: dict[str, list] = {}
    for r in val_rows:
        by_imp.setdefault(r.impression_id, []).append(r)
    imp_by_id = {i.impression_id: i for i in val_imps}

    def scored_metrics(score_fn):
        aucs, mrrs, ndcg5s, ndcg10s = [], [], [], []
        for imp_id, group in by_imp.items():
            clicked = {r.article_id for r in group if r.clicked}
            if not clicked:
                continue
            scores = score_fn(imp_id, group)
            if scores is None:
                continue
            ranked = sorted(group, key=lambda r: -scores.get(r.article_id, float("-inf")))
            ids = [r.article_id for r in ranked]
            a = auc_metric(ids, clicked)
            if a is None:
                continue
            aucs.append(a)
            mrrs.append(mrr_metric(ids, clicked))
            ndcg5s.append(ndcg_at_k(ids, clicked, 5))
            ndcg10s.append(ndcg_at_k(ids, clicked, 10))
        n = len(aucs)
        return {
            "auc": sum(aucs) / n if n else None,
            "mrr": sum(mrrs) / n if n else None,
            "ndcg5": sum(ndcg5s) / n if n else None,
            "ndcg10": sum(ndcg10s) / n if n else None,
            "n": n,
        }

    def before_score(imp_id, group):
        imp = imp_by_id.get(imp_id)
        if imp is None:
            return None
        hist = histories.get(imp.user_id)
        past = hist.before(imp.time) if hist else []
        history_text = [articles[a].retrieval_text for a in past if a in articles]
        if not history_text:
            return None
        subset = [r.article_id for r in group]
        return base_retriever.score_subset(history_text, subset)

    def after_score(imp_id, group):
        return {r.article_id: rerank_scores.get((r.impression_id, r.article_id), float("-inf")) for r in group}

    before = scored_metrics(before_score)
    after = scored_metrics(after_score)
    return {"before_base_retriever_score": before, "after_reranker": after}


if __name__ == "__main__":
    raise SystemExit(main())
