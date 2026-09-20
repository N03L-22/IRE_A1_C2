"""Q5: extend A1's harness rather than building a parallel eval stack.

A1's ``eval.harness.evaluate(retriever, ...)`` is the one place every metric
(recall@K, AUC, MRR, nDCG@5/@10, diversity, novelty, coverage), both slices
(cold/warm, head/tail), and every bootstrap CI are computed -- "there is
nowhere else to compute a metric" (harness.py's own docstring). A re-ranker
does not naturally satisfy the ``Retriever`` protocol (it scores a supplied
candidate SET using behavioural features, it does not rank the whole corpus
from raw history text), so ``RerankedRetriever`` below adapts one: internally
it runs the base retriever for corpus-wide candidates exactly as Q2 does,
builds Q1's features over them, and scores with the trained booster -- then
presents the result as ``(id, score)`` pairs like any other retriever. That
makes ``evaluate()`` usable UNCHANGED for the re-ranked pipeline, which is
what "extend the evaluation harness" (Q5) means here rather than a rewrite.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_A1_ROOT = Path(__file__).resolve().parents[2] / "Assignment-1-Lexical-Semantic-Retrieval"
if str(_A1_ROOT) not in sys.path:
    sys.path.insert(0, str(_A1_ROOT))

from src.data.schema import Article  # noqa: E402

from .candidates import DEFAULT_TOP_K  # noqa: E402
from .features import CandidateFeatures, SessionTracker, build_candidate_rows  # noqa: E402
from .reranker import score_reranker  # noqa: E402


class RerankedRetriever:
    """Wraps a base ``Retriever`` + a trained re-ranker as one ``Retriever``.

    Satisfies the protocol (``name``, ``index``, ``retrieve``) so
    ``eval.harness.evaluate()`` scores it exactly like BM25 or semantic or
    ``RRFusion`` -- Q4.5/Q5 is satisfied by construction, the same way A1's
    own fusion classes are (see fusion.py's docstring).
    """

    def __init__(
        self,
        base_retriever,
        booster,
        feature_names: list[str],
        articles: dict[str, Article],
        train_popularity: dict[str, float],
        top_k: int = DEFAULT_TOP_K,
        name: str | None = None,
    ) -> None:
        self.base = base_retriever
        self.booster = booster
        self.feature_names = feature_names
        self.articles = articles
        self.train_popularity = train_popularity
        self.top_k = top_k
        self.name = name or f"rerank({base_retriever.name})"
        # A single session tracker persisting across calls within one
        # evaluation run -- correct ONLY if the harness calls retrieve() in
        # time order per user, which is how A1's measure() iterates
        # (impressions are read from the dataset reader in file order, which
        # both readers emit chronologically -- the same assumption
        # candidates.build_rerank_table already relies on).
        self._sessions = SessionTracker()
        # retrieval_text -> article_id. Built at index() time so retrieve()
        # can recover ids from the text-only call the harness makes -- see
        # below for why this inversion is necessary rather than optional.
        self._id_by_text: dict[str, str] = {}
        #: Set once per retrieve() call so per-CANDIDATE feature building
        #: (category match, freshness, popularity) has the ids it needs.
        #: recency/session features degrade to "unknown" inside retrieve()
        #: specifically -- see the docstring below -- which is a real,
        #: documented limitation of adapting a Retriever this way, not a bug.
        self._at_time: datetime | None = None

    def index(self, articles: list[Article]) -> None:
        self.base.index(articles)
        for a in articles:
            self._id_by_text[a.retrieval_text] = a.article_id

    def retrieve(
        self, history_text: list[str], k: int, at_time: datetime | None = None
    ) -> list[tuple[str, float]]:
        """Adapts the harness's ``(history_text, k, at_time)`` call shape to
        the re-ranker's id-keyed features.

        A1's ``measure()`` calls every retriever the same way:
        ``texts = [articles[a].retrieval_text for a in past]`` -- text, not
        ids, by design (the ``Retriever`` protocol is dataset-agnostic and
        content-only). The re-ranker's Q1 features need ids for
        category/popularity/freshness lookups, so this recovers them via the
        ``retrieval_text -> article_id`` map built in ``index()``.

        **Known, stated degradation, not silently patched over**: recency
        decay and session-count need PER-CLICK TIMESTAMPS and the impression
        object itself, neither of which the harness's call shape carries
        (only flat text + one ``at_time`` for the whole history). Those two
        features fall back to zero for every candidate scored through THIS
        method -- ``recency_weighted_click_count=0`` (equivalent to "no
        history" for that term) and ``session_click_count=0``. That is
        different from, and weaker than, ``score_slate`` below, which is
        given the real ``Impression`` and computes every feature correctly.
        This method exists so ``eval.harness.evaluate()`` can score SOME
        version of the re-ranker for Q5's recall@K/diversity/novelty/
        coverage bookkeeping (which do not depend on the degraded
        features). Q2/Q3's own before/after and ablation comparisons in
        ``scripts/run_a2.py`` call ``reranker.score_reranker`` directly over
        a full candidate table, not through either method on this class;
        ``score_slate`` is used by ``scripts/predict_submission.py`` for
        the Codabench prediction path, one impression at a time.
        """
        self._at_time = at_time
        base_scored = self.base.retrieve(history_text, k, at_time)
        candidate_ids = [aid for aid, _ in base_scored]
        retrieval_score_by_article = dict(base_scored)
        history_ids = [self._id_by_text.get(t) for t in history_text]
        history_ids = [a for a in history_ids if a is not None]

        history_categories = {
            self.articles[a].category
            for a in history_ids
            if a in self.articles and self.articles[a].category
        }
        X = []
        for aid in candidate_ids:
            art = self.articles.get(aid)
            freshness = None
            if art is not None and art.published_time is not None and at_time is not None:
                freshness = (at_time - art.published_time).total_seconds() / 3600.0
            X.append(
                CandidateFeatures(
                    impression_id="__harness__",
                    user_id="__harness__",
                    article_id=aid,
                    clicked=0,
                    history_click_count=len(history_ids),
                    recency_weighted_click_count=0.0,  # see docstring: unavailable here
                    category_match=int(bool(art) and art.category in history_categories),
                    display_position=None,  # not the real displayed slate here; see docstring
                    session_click_count=0,  # see docstring: unavailable here
                    article_popularity=self.train_popularity.get(aid, 0.0),
                    freshness_hours=freshness,
                    retrieval_score=retrieval_score_by_article.get(aid),
                )
            )
        if not X:
            return []
        scores = score_reranker(self.booster, self.feature_names, X)
        ranked = sorted(candidate_ids, key=lambda aid: -scores.get(("__harness__", aid), float("-inf")))
        return [(aid, scores[("__harness__", aid)]) for aid in ranked]

    def score_slate(self, imp, past_click_ids: list[str]) -> dict[str, float]:
        """The actual re-ranked score for one impression's candidate set.

        This is what Q2/Q3's evaluation and Q4's benchmark call -- it needs
        the real ``Impression`` (for ids, time, clicked) and truncated
        history ids, not just retrieval text, because Q1's features are
        id-keyed (category match, popularity, freshness).

        Returns scores keyed by ``article_id`` alone. ``score_reranker``
        itself keys its result by ``(impression_id, article_id)`` (it
        scores a whole candidate TABLE spanning many impressions at once,
        so the impression id disambiguates); this method scores a single
        impression's rows, so the impression id is redundant on every key
        and is dropped here. Regression note: an earlier version returned
        the raw ``(impression_id, article_id)``-keyed dict, which made
        every consumer expecting bare article ids (rank_candidates in
        predict_submission.py) silently miss every lookup and fall back to
        an unscored identity ordering -- caught by inspecting a real
        prediction file: 200/200 lines came out as [1,2,3,...,N].
        """
        session_count = self._sessions.click_count_before(imp)
        self._sessions.observe(imp)
        history_text = [self.articles[a].retrieval_text for a in past_click_ids if a in self.articles]
        # base.score_subset ranks the SLATE directly (A1's fusion.py: "RRF
        # needs only ranks, and each component can rank a slate without a
        # full retrieval"), which is both cheaper than a full corpus
        # retrieval and gives retrieval_score for every candidate ACTUALLY
        # in imp.candidates -- unlike retrieve(), which only scores what it
        # happened to retrieve top-K from the whole corpus.
        retrieval_score_by_article = (
            self.base.score_subset(history_text, imp.candidates) if history_text else {}
        )
        # Safe here for the same reason it's safe in
        # candidates.build_rerank_table_on_slate: imp.candidates IS the real
        # displayed slate at serving time too, so this must be populated
        # whenever the booster was trained with display_position as a
        # feature (train_reranker_for_submission's extra_features) -- an
        # empty dict here would silently starve that feature to NaN on
        # every real prediction, not a leak but a wasted/broken feature.
        display_position_by_article = {aid: i for i, aid in enumerate(imp.candidates, start=1)}
        rows = build_candidate_rows(
            imp, past_click_ids, None, self.articles, self.train_popularity,
            session_click_count=session_count,
            display_position_by_article=display_position_by_article,
            retrieval_score_by_article=retrieval_score_by_article,
        )
        keyed = score_reranker(self.booster, self.feature_names, rows)
        return {aid: keyed[(imp_id, aid)] for (imp_id, aid) in keyed}
