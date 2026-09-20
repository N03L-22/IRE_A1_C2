"""Q2.1: top-K candidate generation, reusing A1's Retriever protocol directly.

A1's ``architecture.md`` calls the ``Retriever`` interface "the load-bearing
design decision" precisely so a later component (this one) could sit on top
without re-deriving retrieval. ``build_candidate_table`` does not retrieve
anything itself -- it takes any already-built ``Retriever`` (BM25, semantic,
or an ``RRFusion`` of both) and turns its corpus-wide ranking into the
top-K candidate list Q2 asks for, merged with Q1's behavioural features and
the ground-truth click label.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_A1_ROOT = Path(__file__).resolve().parents[2] / "Assignment-1-Lexical-Semantic-Retrieval"
if str(_A1_ROOT) not in sys.path:
    sys.path.insert(0, str(_A1_ROOT))

from src.data.schema import Article, History, Impression  # noqa: E402
from src.data.split import truncate_history  # noqa: E402

from .features import (  # noqa: E402
    CandidateFeatures,
    SessionTracker,
    build_candidate_rows,
    train_popularity_from_impressions,
)

#: Q2.1 names this range explicitly ("K ~ 100-200"); moved from 150 to the
#: top of that range -- no brief requirement pinned us to 150, it was only
#: ever this pipeline's own unexamined default, and the top of the stated
#: range gives the re-ranker strictly more candidates to recover from a
#: retrieval miss without changing the candidate-generation strategy itself.
DEFAULT_TOP_K = 200


class UnionRetriever:
    """Q2.1 candidate-generation ablation: SET UNION of each component
    retriever's own top-K/n, instead of RRFusion's rank fusion.

    Not the default -- ``run_a2.py --candidate-generator`` still defaults to
    ``bm25+semantic`` (RRFusion). This exists to answer a specific, narrower
    question: does simply pooling each retriever's independent top slice
    (rather than blending them by reciprocal rank) change what actually
    reaches the re-ranker?

    RRF and union differ in a way that matters here: RRF's score for a
    document depends on ITS RANK IN EVERY COMPONENT (a document ranked 200th
    everywhere still contributes, just weakly), so one weak, mostly-agreeing
    component can quietly reweight the whole fused order. Union instead asks
    each component to contribute a fixed slice outright and just pools the
    ids -- no component can be down-weighted by disagreement, but a document
    only one component ranks highly also gets no fusion boost for agreement.
    Satisfies the same ``Retriever`` protocol (``.name``, ``.index``,
    ``.retrieve``, ``.score_subset``) so every downstream caller in this
    pipeline (``build_rerank_table``, ``RerankedRetriever``) works unchanged.
    """

    def __init__(self, retrievers: list, name: str | None = None) -> None:
        if not retrievers:
            raise ValueError("union needs at least one retriever")
        self.retrievers = retrievers
        self.name = name or "union(" + "+".join(r.name.split("(")[0] for r in retrievers) + ")"

    def index(self, articles) -> None:
        for r in self.retrievers:
            r.index(articles)

    def retrieve(self, history_text: list[str], k: int, at_time=None) -> list[tuple[str, float]]:
        per_component = max(1, k // len(self.retrievers))
        pooled: dict[str, float] = {}
        for retriever in self.retrievers:
            for aid, score in retriever.retrieve(history_text, per_component, at_time):
                # A document more than one component surfaces keeps its
                # highest raw score -- components' scales differ (BM25
                # unbounded, cosine in [-1, 1]), but max() only needs "did
                # any component rank this highly," not a comparable scale,
                # since union's job is membership, not the score value
                # itself (score_subset below is what the re-ranker actually
                # reads for magnitude).
                if aid not in pooled or score > pooled[aid]:
                    pooled[aid] = score
        ranked = sorted(pooled.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:k]

    def score_subset(self, history_text: list[str], subset: list[str]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for retriever in self.retrievers:
            for aid, score in retriever.score_subset(history_text, subset).items():
                if aid not in scores or score > scores[aid]:
                    scores[aid] = score
        return scores


def retrieve_candidates(
    retriever,
    past_click_ids: list[str],
    articles: dict[str, Article],
    imp: Impression,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[list[str], set[str], dict[str, float]]:
    """Top-K corpus-wide candidates for one impression, from any ``Retriever``.

    Falls back to the impression's own slate when the user has no usable
    history (cold start) -- consistent with how A1's harness treats a
    history-less retrieval (``measure()``: empty ``texts`` -> ``[]``), except
    here we still need *something* to rank, so the slate itself becomes the
    candidate set rather than an empty list.

    Returns ``(candidate_ids, appended_ids, scores)``.

    ``appended_ids`` marks which candidates were added because the retriever
    missed the clicked article, NOT ranked by it -- callers must not let
    those ids' position in the returned list become a feature, because "was
    appended" is a proxy for "is clicked" (every append is a click; virtually
    no organically-ranked candidate is). See ``features.build_candidate_rows``'s
    handling of this set: it is why ``position_in_slate`` cannot simply be
    the list index.

    Regression note: an earlier version DID leak clicked-ness through
    position -- the clicked article was always appended last, so
    ``position_in_slate`` became a near-perfect predictor of the label and
    the re-ranker's "before/after" comparison came back at AUC 0.99 for a
    reason that had nothing to do with behavioural features (found via a
    full small-tier dry run: feature_importance showed position_in_slate at
    97% of total gain). The return shape exists specifically so that bug
    class cannot recur silently.

    ``scores`` covers EVERY id in the returned candidate list, appended ones
    included -- this feeds Q1's ``retrieval_score`` feature.

    Regression note, second occurrence of the same bug class: an earlier
    version left appended ids OUT of ``scores`` entirely, so
    ``retrieval_score`` came out ``None``/NaN for them -- and because every
    appended id is a click (by construction), NaN became a near-perfect
    proxy for ``clicked=1`` all over again, through missingness instead of
    through position this time. Offline AUC on a submission-time model
    (article_popularity excluded, so retrieval_score dominated at 98%+ of
    gain) came back at 0.99, and the REAL Codabench leaderboard score for
    that exact submission was 0.5124 -- BELOW even A1's plain, un-reranked
    BM25 baseline (0.5568) on the same competition. The offline number was
    measuring "can the model detect the appended-candidate artifact," not
    real ranking quality. Caught by comparing this submission's leaderboard
    score against A1's own logged submission history
    (``Assignment-1-.../submissions/*.meta.json``), which is exactly why
    that comparison matters and shouldn't be skipped even when the request
    is just "generate a prediction file."

    Fixed by actually scoring appended candidates via
    ``retriever.score_subset`` (the same slate-scoring primitive
    ``_before_after_eval``/``score_slate`` already use) instead of leaving
    them unscored -- every candidate gets a genuine, non-fabricated
    retrieval_score, so NaN never appears and can never correlate with the
    label again.
    """
    texts = [articles[a].retrieval_text for a in past_click_ids if a in articles]
    if not texts:
        return list(imp.candidates), set(), {}
    scored = retriever.retrieve(texts, top_k, imp.time)
    scores = dict(scored)
    ids = [aid for aid, _ in scored]
    truncated = ids[:top_k]
    appended: set[str] = set()
    for aid in imp.clicked:
        if aid not in truncated:
            truncated.append(aid)
            appended.add(aid)
    if appended:
        # score_subset ranks a given slate directly without a full corpus
        # retrieval (A1's fusion.py: "each component can rank a slate
        # without a full retrieval") -- cheap even for a handful of ids.
        appended_scores = retriever.score_subset(texts, list(appended))
        scores.update(appended_scores)
    return truncated, appended, {aid: scores[aid] for aid in truncated if aid in scores}


@dataclass
class RetrievalCoverage:
    """Q2.1's recall@K (and precision@K) for the candidate generator
    actually used to build a rerank table -- how often the true click
    survives retrieval BEFORE the guaranteed append in
    ``retrieve_candidates`` rescues it.

    This is a hard, explicit ceiling on the whole pipeline that was
    previously never measured here: the guaranteed append means every
    training row the re-ranker ever sees HAS the click (by construction, see
    Bug 1/§retrieve_candidates), so recall is invisible from the training
    table itself; it has to be counted at retrieval time, before the append,
    which is exactly what this does.

    ``precision_at_k`` is also tracked, mostly to state plainly what it
    reduces to here: with (per A1's own F7) 99.5% single-click impressions,
    a hit contributes 1/K and a miss contributes 0, so
    precision@K = recall@K / K exactly whenever an impression has at most
    one click -- it carries no information recall@K doesn't already have on
    this dataset. Reported anyway because the course names Precision@K and
    Recall@K as a paired standard measure; the redundancy itself is the
    honest finding for a single-relevant-item retrieval task, not a reason
    to omit the number.
    """

    impressions_checked: int = 0
    impressions_with_click_retrieved: int = 0
    #: sum of (# retrieved-and-clicked / K) across impressions_checked --
    #: precision@K's numerator needs K itself (unlike recall@K), so this
    #: accumulates the per-impression fraction directly rather than a count.
    precision_at_k_sum: float = 0.0

    @property
    def recall_at_k(self) -> float:
        return (
            self.impressions_with_click_retrieved / self.impressions_checked
            if self.impressions_checked else 0.0
        )

    @property
    def precision_at_k(self) -> float:
        return (
            self.precision_at_k_sum / self.impressions_checked
            if self.impressions_checked else 0.0
        )


def build_rerank_table(
    retriever,
    impressions: Iterable[Impression],
    histories: dict[str, History],
    articles: dict[str, Article],
    train_popularity: dict[str, float],
    top_k: int = DEFAULT_TOP_K,
    max_impressions: int | None = None,
    coverage: RetrievalCoverage | None = None,
) -> list[CandidateFeatures]:
    """The Q2 training/eval table: one row per (impression, candidate).

    Each impression's candidate set comes from ``retriever`` (Q2.1's top-K),
    not from the impression's own displayed slate -- that is what makes this
    a genuine two-stage retrieve-then-rank pipeline rather than just
    re-scoring the dataset's pre-built slate.

    ``impressions`` must be in time order (both readers already emit them
    that way -- see ``truncate_history``'s own reliance on the same
    assumption) so ``SessionTracker`` only ever sees a user's PRIOR
    impressions when computing session_click_count for the current one.

    ``display_position`` (Q1.2's genuine position-bias feature) is taken
    from ``imp.candidates`` BEFORE it is replaced by the retrieved set --
    that original order is the real, dataset-displayed slate the user saw.
    Candidates the retriever pulls in that were NOT in that original slate
    (i.e. every retrieval hit beyond the original small candidate list) get
    no display position (``None`` -> NaN), which is correct: they were never
    actually shown to this user, so no position-bias signal exists for them.

    ``coverage``, if given, is updated in place with recall@K (see
    ``RetrievalCoverage``) -- pass the same instance across calls to
    accumulate over train+val, or a fresh one to measure a single split.
    """
    rows: list[CandidateFeatures] = []
    sessions = SessionTracker()
    n = 0
    for imp in impressions:
        if not imp.is_labelled:
            sessions.observe(imp)  # still contributes to session history, even if unscored
            continue
        hist = histories.get(imp.user_id)
        if hist is None:
            sessions.observe(imp)
            continue
        past = hist.before(imp.time)
        display_position_by_article = {aid: i for i, aid in enumerate(imp.candidates, start=1)}
        candidate_ids, appended, retrieval_score_by_article = retrieve_candidates(
            retriever, past, articles, imp, top_k
        )
        if coverage is not None and past:  # only meaningful once the retriever actually ran
            coverage.impressions_checked += 1
            clicked = set(imp.clicked)
            # "retrieved" means the retriever's own top-K found it organically,
            # i.e. it did NOT need to be rescued by the append-on-miss path.
            organic_hits = clicked & set(candidate_ids) - appended
            if organic_hits:
                coverage.impressions_with_click_retrieved += 1
            # precision@K's numerator: how many of the K organically-retrieved
            # slots were actually the click (appended ones are not part of the
            # retriever's own top-K, so they don't belong in this count).
            coverage.precision_at_k_sum += len(organic_hits) / top_k
        session_count = sessions.click_count_before(imp)
        sessions.observe(imp)

        # Build a synthetic impression whose "candidates" are the RETRIEVED
        # set, not the original slate -- Q1's feature builder is candidate-set
        # agnostic, it just needs an Impression shape to read imp.time/clicked
        # from and a list of candidate ids to emit rows for.
        synthetic = Impression(
            impression_id=imp.impression_id,
            user_id=imp.user_id,
            time=imp.time,
            candidates=candidate_ids,
            clicked=imp.clicked,
            session_id=imp.session_id,
        )
        history_times_by_id = None
        if hist.is_verifiable:
            history_times_by_id = dict(zip(hist.clicked_ids, hist.times))
        rows.extend(
            build_candidate_rows(
                synthetic, past, history_times_by_id, articles, train_popularity,
                session_click_count=session_count,
                display_position_by_article=display_position_by_article,
                retrieval_score_by_article=retrieval_score_by_article,
            )
        )
        n += 1
        if max_impressions and n >= max_impressions:
            break
    return rows


def build_rerank_table_on_slate(
    retriever,
    impressions: Iterable[Impression],
    histories: dict[str, History],
    articles: dict[str, Article],
    train_popularity: dict[str, float],
    max_impressions: int | None = None,
) -> list[CandidateFeatures]:
    """Q2's training table, built over the REAL displayed slate
    (``imp.candidates`` as-is) instead of a corpus-wide top-K retrieval --
    mirrors exactly what ``RerankedRetriever.score_slate`` does at
    submission/serving time.

    Why this exists alongside ``build_rerank_table``: found via a real
    Codabench regression (the re-ranker submission scored 0.5081, BELOW
    A1's own plain-BM25 leaderboard baseline of 0.5568, even after the
    retrieval_score-missingness leak was fixed). The root cause was a
    train/serve candidate-set mismatch, not a leak: ``build_rerank_table``
    trains on corpus-wide top-150 candidate sets (mean size ~150 on MIND),
    while ``score_slate`` scores the real displayed slate at serving time
    (mean size ~36 on MIND, measured directly) -- a ~4x difference in
    candidate-set size that also changes retrieval_score's distribution
    (BM25 over a real ~36-item slate scores very differently than BM25's
    top-150 out of the whole 65K-article corpus). A LightGBM lambdarank
    model trained on one distribution and scored on a systematically
    different one is exactly the setup that degrades silently offline
    (whatever validation table is used has the SAME mismatch as training)
    and only shows up against ground truth the model never saw during
    development -- the real leaderboard.

    This function trains on the same object the submission scores against,
    so there is no distribution shift between training and serving left to
    find. It does not do "retrieval" in the Q2.1 sense at all -- the whole
    point is to match serving exactly, not to add candidate-generation
    signal on top of it. ``predict_submission.py`` should use this to train
    the SUBMISSION model; ``run_a2.py``'s offline Q2/Q3/Q5 evaluation
    should keep using ``build_rerank_table`` unchanged, because THAT one
    genuinely needs Q2.1's corpus-wide retrieve-then-rank design (the whole
    point being to retrieve candidates the impression's own slate does not
    already contain).
    """
    rows: list[CandidateFeatures] = []
    sessions = SessionTracker()
    n = 0
    for imp in impressions:
        if not imp.is_labelled:
            sessions.observe(imp)
            continue
        hist = histories.get(imp.user_id)
        if hist is None:
            sessions.observe(imp)
            continue
        past = hist.before(imp.time)
        texts = [articles[a].retrieval_text for a in past if a in articles]
        retrieval_score_by_article = (
            retriever.score_subset(texts, imp.candidates) if texts else {}
        )
        # Safe to use here, UNLIKE build_rerank_table: every candidate IS a
        # real slate position now (imp.candidates used as-is, no corpus-wide
        # retrieval and no append-on-miss), so position no longer correlates
        # with organic-vs-appended -- there is no "appended" category in
        # this construction. See candidates.py's FIELDS-exclusion comment in
        # features.py for why it's unsafe in build_rerank_table specifically.
        display_position_by_article = {aid: i for i, aid in enumerate(imp.candidates, start=1)}
        session_count = sessions.click_count_before(imp)
        sessions.observe(imp)

        history_times_by_id = None
        if hist.is_verifiable:
            history_times_by_id = dict(zip(hist.clicked_ids, hist.times))
        rows.extend(
            build_candidate_rows(
                imp, past, history_times_by_id, articles, train_popularity,
                session_click_count=session_count,
                display_position_by_article=display_position_by_article,
                retrieval_score_by_article=retrieval_score_by_article,
            )
        )
        n += 1
        if max_impressions and n >= max_impressions:
            break
    return rows


__all__ = [
    "DEFAULT_TOP_K",
    "RetrievalCoverage",
    "UnionRetriever",
    "retrieve_candidates",
    "build_rerank_table",
    "build_rerank_table_on_slate",
    "train_popularity_from_impressions",
]
