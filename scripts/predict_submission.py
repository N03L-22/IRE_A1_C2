"""Q5: generate Codabench prediction files for the RE-RANKED A2 pipeline.

Same on-disk format as A1's src/submit/codabench.py (reused deliberately,
not reinvented -- see that module's docstring for why the format details
below are exact, not approximate):

    impression_id [rank1,rank2,...,rankN]

One line per impression; rank list is a permutation of 1..N aligned to the
candidate list IN THE ORDER GIVEN (rank 1 = most likely click). The archive
member name inside the zip differs by exactly one letter between the two
competitions and getting it wrong burns a submission from the daily quota
(A1 F35) -- SUBMISSION_MEMBER below is copied from A1's already-verified
mapping, not re-derived.

Unlike A1's script, this one scores through the trained Q2 LightGBM
re-ranker (RerankedRetriever.score_slate), not a bare retriever -- the
whole point of A2's submission is that it reflects the behavioural
features, not just BM25/semantic candidate order.

    .venv/bin/python -m scripts.predict_submission --dataset ebnerd \
        --model-checkpoint results/reranker_ebnerd.pkl --dev-limit 2000
    .venv/bin/python -m scripts.predict_submission --dataset mind \
        --model-checkpoint results/reranker_mind.pkl   # full test set

Serial by design (unlike A1's ProcessPoolExecutor path) -- correctness
first at this stage; --dev-limit lets you verify the output on a slice
before committing to a multi-hour full run over 2.37M/13.5M impressions.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

_A2_ROOT = Path(__file__).resolve().parents[1]
_A1_ROOT = _A2_ROOT.parent / "Assignment-1-Lexical-Semantic-Retrieval"
sys.path.insert(0, str(_A2_ROOT))
sys.path.insert(0, str(_A1_ROOT))

from src.data.readers import SPLIT_NAMES, get_reader  # noqa: E402
from src.resources import InsufficientResources, add_arguments, from_args  # noqa: E402
from src.retrieval.bm25 import BM25Retriever  # noqa: E402
from src.retrieval.fusion import RRFusion  # noqa: E402
from src.retrieval.semantic import SemanticRetriever  # noqa: E402
from src.retrieval.encode import encode_cached  # noqa: E402

from a2src.rerank.candidates import (  # noqa: E402
    DEFAULT_TOP_K,
    UnionRetriever,
    build_rerank_table_on_slate,
    train_popularity_from_impressions,
)
from a2src.rerank.reranked_retriever import RerankedRetriever  # noqa: E402
from a2src.rerank.reranker import train_reranker  # noqa: E402

log = logging.getLogger("predict_submission")

#: Copied verbatim from A1's src/submit/codabench.py -- verified against
#: both upstream scorers there; not re-derived here.
SUBMISSION_MEMBER = {
    "mind": "prediction.txt",
    "ebnerd": "predictions.txt",
}
LEADERBOARD_URL = {
    "mind": "https://www.codabench.org/competitions/13967/",
    "ebnerd": "https://www.codabench.org/competitions/2469/",
}
#: The tier holding the TEST split -- asymmetric between datasets, and not
#: fixable by one shared --test-tier default. MIND's large_test lives under
#: the "large" tier (alongside large train/dev, per A1's extract.py). EB-NeRD's
#: unlabelled test set was extracted from a SEPARATE archive (ebnerd_testset.zip)
#: into its own "testset" tier -- "large" only has train/validation there (A1's
#: extract.py comment: "ebnerd_large.zip ships train/ and validation/ only").
#: A run with --test-tier large on ebnerd fails with FileNotFoundError at the
#: histories() call, after the (expensive) re-ranker training step has already
#: completed -- found by running the real full-scale prediction job.
DEFAULT_TEST_TIER = {
    "mind": "large",
    "ebnerd": "testset",
}
BM25_PARAMS = {
    "ebnerd": dict(k1=1.6, b=1.0, last_n=15),
    "mind": dict(k1=1.6, b=0.75, last_n=5),
}


def rank_candidates(candidates: list[str], scores: dict[str, float]) -> list[int]:
    """One rank per candidate, in ORIGINAL slate order -- A1's function,
    copied verbatim (codabench.py's own docstring: F21 found an arbitrary
    tie-break over a mostly-unscored slate silently turns a baseline into a
    slate-order baseline; deterministic tie-break by original position
    avoids that)."""
    order = sorted(
        range(len(candidates)),
        key=lambda i: (-scores.get(candidates[i], float("-inf")), i),
    )
    ranks = [0] * len(candidates)
    for rank, idx in enumerate(order, start=1):
        ranks[idx] = rank
    return ranks


def train_reranker_for_submission(dataset: str, args) -> RerankedRetriever:
    """Train the Q2 re-ranker on the TRAIN split (never the test split --
    the test split has no click labels to train on anyway, and reusing any
    held-out split here would be the Q9 anti-gaming violation the brief
    explicitly calls out).

    Trains on ``build_rerank_table_on_slate`` -- the REAL displayed slate,
    not a corpus-wide top-K retrieval -- because a real Codabench
    submission (0.5124 AUC, later 0.5081 after an unrelated leak fix) came
    in BELOW A1's own plain-BM25 leaderboard baseline (0.5568) on the
    identical competition. Root cause, found only after checking against
    A1's own submission history: a train/serve candidate-set mismatch.
    ``build_rerank_table`` (the old path here) trains on corpus-wide top-150
    retrieval (mean candidate-set size ~150 on MIND); the actual submission
    scores the real slate at serving time (mean size ~36, measured
    directly) via ``RerankedRetriever.score_slate``. A model trained on one
    distribution and deployed on a systematically different one degrades
    silently offline -- the validation table has the SAME mismatch as
    training -- and only shows up against data the model never saw in any
    form during development, i.e. the real leaderboard. Confirmed directly:
    scoring the REAL slate with plain BM25 order gives offline AUC 0.5589,
    matching A1's actual leaderboard score (0.5568) almost exactly, versus
    the old corpus-wide construction's offline AUC of 0.32 for the
    "before" number -- the old offline evaluation was measuring the wrong
    task entirely.

    ``article_popularity`` stays excluded (small-tier train / large-tier
    test corpora are largely disjoint article sets -- even the FULL
    small-tier train split covers only 11.4% of MIND-large's test corpus,
    1.5% of EB-NeRD's -- so it degrades to near-constant at submission
    time). ``display_position`` is INCLUDED here, unlike in
    ``build_rerank_table``'s output: it is unsafe there (leaks
    organic-vs-appended candidate status through position, see
    ``candidates.py``), but safe here, because every candidate in a
    slate-trained row IS a real slate position -- there is no retrieval
    simulation and no append-on-miss in this construction, so nothing
    about a candidate's presence correlates with it being the click.
    """
    reader = get_reader(dataset, args.work_dir, args.tier)
    split = SPLIT_NAMES[dataset]["train"]
    articles = {a.article_id: a for a in reader.articles()}
    histories = {h.user_id: h for h in reader.histories(split)}
    train_imps = []
    for imp in reader.impressions(split):
        train_imps.append(imp)
        if len(train_imps) >= args.train_limit:
            break
    log.info("  training re-ranker on %d %s train impressions", len(train_imps), dataset)
    train_popularity = train_popularity_from_impressions(train_imps)

    bm25_params = BM25_PARAMS[dataset]
    bm25 = BM25Retriever(**bm25_params)
    bm25.index(list(articles.values()))
    if args.candidate_generator == "bm25":
        candidate_gen = bm25
    else:
        ids = [a.article_id for a in articles.values()]
        texts = [a.retrieval_text for a in articles.values()]
        vecs, _ = encode_cached(texts, ids, cache_dir=str(args.embed_cache_dir))
        semantic = SemanticRetriever(
            vectors=vecs, vector_ids=ids,
            tau=args.semantic_tau, decay=args.semantic_decay,
        )
        semantic.index(list(articles.values()))
        if args.candidate_generator == "bm25+semantic+union":
            # Ablation (not the default): SET UNION of BM25's and Semantic's
            # own top-K/2 each, instead of RRFusion's rank fusion -- see
            # candidates.UnionRetriever's docstring. Note this only affects
            # `score_subset`'s use here (slate-based training/serving never
            # calls `retrieve`), so the difference from RRFusion is entirely
            # in how the two components' per-slate scores get combined.
            candidate_gen = UnionRetriever([bm25, semantic])
        else:
            candidate_gen = RRFusion([bm25, semantic])
        candidate_gen.index(list(articles.values()))

    train_rows = build_rerank_table_on_slate(
        candidate_gen, train_imps, histories, articles, train_popularity,
        max_impressions=args.train_limit,
    )
    extra = ["display_position"] if args.use_display_position else []
    booster, feature_names = train_reranker(
        train_rows, num_boost_round=args.num_boost_round,
        exclude_features=["article_popularity"],
        extra_features=extra,
    )
    log.info("  re-ranker trained: %d rows, %d features (slate-trained, article_popularity excluded, "
              "display_position %s -- see docstring)", len(train_rows), len(feature_names),
              "included" if args.use_display_position else "excluded")
    return RerankedRetriever(candidate_gen, booster, feature_names, articles, train_popularity, top_k=args.top_k)


def build_predictions(
    dataset: str,
    reranked: RerankedRetriever,
    test_articles: dict,
    test_histories: dict,
    reader,
    test_split: str,
    out_path: Path,
    dev_limit: int | None = None,
    log_every: int = 50_000,
) -> dict:
    """Stream the TEST split (unlabelled -- imp.clicked is always empty
    there), writing one prediction line per impression. Deduplicates
    repeated impression ids the same way A1's build_predictions does
    (EB-NeRD's test set repeats ~200K ids)."""
    written = 0
    cold_slates = 0
    seen: set[str] = set()
    started = time.perf_counter()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for imp in reader.impressions(test_split):
            if imp.impression_id in seen:
                continue
            seen.add(imp.impression_id)

            hist = test_histories.get(imp.user_id)
            past = hist.before(imp.time) if hist is not None else []
            if past:
                scores = reranked.score_slate(imp, past)
            else:
                scores = {}
                cold_slates += 1

            ranks = rank_candidates(imp.candidates, scores)
            f.write(f"{imp.impression_id} [{','.join(map(str, ranks))}]\n")
            written += 1
            if written % log_every == 0:
                rate = written / (time.perf_counter() - started)
                log.info("  %s lines written (%.0f/s)", f"{written:,}", rate)
            if dev_limit and written >= dev_limit:
                break

    elapsed = time.perf_counter() - started
    return {
        "lines": written,
        "cold_slates": cold_slates,
        "seconds": round(elapsed, 1),
        "bytes": out_path.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", choices=["mind", "ebnerd"], required=True)
    p.add_argument("--tier", default="small", help="tier the RE-RANKER is TRAINED on (small)")
    p.add_argument("--test-tier", default=None,
                    help="tier the TEST predictions are generated FROM "
                         "(default: DEFAULT_TEST_TIER[dataset] -- 'large' for mind, "
                         "'testset' for ebnerd; the two are NOT interchangeable, see "
                         "DEFAULT_TEST_TIER's docstring)")
    p.add_argument("--work-dir", type=Path, default=_A1_ROOT / "data" / "work")
    p.add_argument("--embed-cache-dir", type=Path, default=_A1_ROOT / "data" / "store" / "embeddings")
    p.add_argument("--out-dir", type=Path, default=_A2_ROOT / "submissions")
    p.add_argument("--train-limit", type=int, default=30000)
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--num-boost-round", type=int, default=200)
    p.add_argument("--candidate-generator", choices=["bm25", "bm25+semantic", "bm25+semantic+union"], default="bm25")
    p.add_argument("--semantic-tau", type=float, default=0.2,
                    help="SemanticRetriever coherence threshold -- default 0.2 matches A1's "
                         "own tuned value for its best MIND fusion submission "
                         "(mind_fusion_22aaef_i1, AUC 0.6047), NOT the library default of 0.35 "
                         "-- see Assignment-1-.../submissions/mind_fusion_22aaef_i1_prediction.meta.json")
    p.add_argument("--semantic-decay", choices=["log", "flat"], default="flat",
                    help="SemanticRetriever history-weighting decay -- default 'flat' matches "
                         "A1's tuned value for the same submission, not the library default 'log'")
    p.add_argument("--use-display-position", action="store_true", default=True,
                    help="include display_position as a feature (default on; safe under "
                         "slate-based training -- see train_reranker_for_submission's docstring)")
    p.add_argument("--no-display-position", dest="use_display_position", action="store_false")
    p.add_argument("--dev-limit", type=int, default=None,
                    help="cap test impressions for a fast correctness check before a full run")
    add_arguments(p)  # A1's --n-jobs/--mem-gb/--ignore-availability (src/resources.py)
    args = p.parse_args(argv)
    if args.test_tier is None:
        args.test_tier = DEFAULT_TEST_TIER[args.dataset]

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("bm25s").setLevel(logging.ERROR)

    # This script holds the entire small-tier train impression set, all
    # candidate rows built from it (millions), the trained booster, and then
    # streams a 2.37M/13.5M-line test split -- a genuinely heavy footprint,
    # and this pipeline had NO memory check at all before this. Refuse to
    # start on a machine already tight on RAM rather than launching into one.
    try:
        budget = from_args(args)
    except InsufficientResources as exc:
        log.error("%s", exc)
        return 3
    log.info("%s", budget)

    dataset = args.dataset

    # Fail fast on a bad --test-tier BEFORE the expensive training step below
    # (7+ minutes at full small-tier scale) -- found the hard way: an EB-NeRD
    # run crashed on FileNotFoundError from the test split AFTER training had
    # already finished, because "large" (this script's old blanket default)
    # only has train/validation for EB-NeRD; its test split lives under a
    # separate "testset" tier. This existence check costs nothing and turns
    # that failure into an immediate, cheap one.
    #
    # The probe path itself must match get_reader's OWN construction, which
    # differs by dataset: EbnerdReader nests by tier (work_dir/ebnerd/{tier}/
    # {split}); MindReader does not (work_dir/mind/{split} always, tier
    # ignored entirely by A1's reader -- see readers.get_reader). Probing a
    # tier-nested path for MIND would falsely fail here.
    test_split = SPLIT_NAMES[dataset]["test"]
    if dataset == "ebnerd":
        probe_dir = args.work_dir / "ebnerd" / args.test_tier / test_split
    else:
        probe_dir = args.work_dir / "mind" / test_split
    if not probe_dir.exists():
        log.error(
            "test split not found at %s -- check --test-tier (default for "
            "%s is %r; the two datasets are NOT interchangeable, see "
            "DEFAULT_TEST_TIER's docstring)",
            probe_dir, dataset, DEFAULT_TEST_TIER[dataset],
        )
        return 2

    log.info("=== %s: training re-ranker (%s tier, %s candidates) ===",
              dataset, args.tier, args.candidate_generator)
    reranked = train_reranker_for_submission(dataset, args)

    log.info("=== %s: loading TEST split (%s tier) ===", dataset, args.test_tier)
    test_reader = get_reader(dataset, args.work_dir, args.test_tier)
    try:
        test_articles = {a.article_id: a for a in test_reader.articles(splits=(test_split,))}
    except TypeError:
        test_articles = {a.article_id: a for a in test_reader.articles()}
    log.info("  %d test-split articles", len(test_articles))
    test_histories = {h.user_id: h for h in test_reader.histories(test_split)}
    log.info("  %d test-split users with history", len(test_histories))

    # The re-ranker's article/popularity lookups were built from the TRAIN
    # corpus (train_reranker_for_submission) -- swap in the test corpus so
    # freshness/category features resolve against the articles actually in
    # the test slates, exactly as A1's submission path re-indexes onto the
    # test split's own corpus (F14: an article absent from the index can
    # never be scored).
    reranked.articles = test_articles

    # Bug found comparing A2's real leaderboard score (0.5346, v7) against
    # A1's own plain-BM25 score (0.5568) on the identical MIND competition:
    # this line used to swap ONLY the plain `articles` dict above, never the
    # candidate generator's own index -- so `reranked.base` (the BM25/fusion
    # retriever actually used by `score_slate` -> `score_subset`) stayed
    # indexed on the small TRAIN corpus (65,238 articles on MIND) while the
    # real test slates draw from the large TEST corpus (120,961 articles).
    # `BM25Retriever.score_subset`'s own docstring says candidates absent
    # from its index are simply missing from the returned scores -- so for
    # every test-slate candidate published outside the small-tier training
    # window (measured: covers only 11.4% of MIND-large's test corpus, the
    # same coverage gap already documented as the reason `article_popularity`
    # is excluded above), `retrieval_score` silently came back undefined and
    # that candidate fell to `rank_candidates`'s original-slate-order
    # tie-break instead of a real BM25 rank. A1's own submission script
    # (`src/submit/codabench.py`) never hits this because it indexes and
    # scores on the SAME tier (`--tier` defaults to `large` there, matching
    # the test split) -- there was never a train/test split of tiers for it
    # to get out of sync in the first place.
    #
    # Re-indexing here (BM25Retriever.index() fully rebuilds its internal
    # state each call, so this is safe to call a second time) closes that
    # gap for BM25 and RRFusion. Note: if `--candidate-generator` includes
    # semantic search, `SemanticRetriever.index()` behaves differently when
    # constructed from pre-computed vectors (the `vectors=`/`vector_ids=`
    # path this script uses) -- it FILTERS the already-encoded train vectors
    # down to whichever ids overlap the new article set, rather than
    # re-encoding the test corpus, so it would suffer the same 11.4%-overlap
    # problem even after this fix. The only candidate generator with real
    # Codabench evidence behind it (v7, plain BM25) is unaffected by that
    # caveat; a semantic/fusion submission would need `SemanticRetriever` to
    # re-encode the test corpus properly, which is out of scope here.
    reranked.index(list(test_articles.values()))

    pos_tag = "pos" if args.use_display_position else "nopos"
    # top_k is part of the filename (not just candidate_generator/pos_tag)
    # because K changed 150->200 mid-pipeline without changing the generator
    # name -- a same-named rerun at the new K would silently overwrite the
    # old K=150 submission files with no trace of which K produced which
    # result (the exact "make the output files different" bug this pipeline
    # already hit once with v4/v5, see the tracking note).
    out_stem = f"{dataset}_a2_rerank_{args.candidate_generator.replace('+', '')}_k{args.top_k}_{pos_tag}"
    txt_path = args.out_dir / f"{out_stem}_prediction.txt"
    log.info("=== %s: streaming predictions -> %s ===", dataset, txt_path)
    stats = build_predictions(
        dataset, reranked, test_articles, test_histories, test_reader, test_split,
        txt_path, dev_limit=args.dev_limit,
    )
    log.info("wrote %s: %s lines, %.2f MB, %.1fs (%s cold slates)",
              txt_path, f"{stats['lines']:,}", stats["bytes"] / 1e6, stats["seconds"],
              f"{stats['cold_slates']:,}")

    zip_path = args.out_dir / f"{out_stem}_prediction.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(txt_path, arcname=SUBMISSION_MEMBER[dataset])
    log.info("zipped -> %s (%.2f MB)", zip_path, zip_path.stat().st_size / 1e6)

    meta_path = args.out_dir / f"{out_stem}_prediction.meta.json"
    meta_path.write_text(json.dumps({
        "dataset": dataset,
        "candidate_generator": args.candidate_generator,
        "train_tier": args.tier,
        "train_limit": args.train_limit,
        "test_tier": args.test_tier,
        "dev_limit": args.dev_limit,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **stats,
    }, indent=2))
    log.info("metadata -> %s", meta_path)
    log.info("\nupload %s to %s", zip_path.name, LEADERBOARD_URL[dataset])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
