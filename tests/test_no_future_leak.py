"""Q9 for A2's new behavioural features: no future click may reach a feature.

Follows A1's tests/test_no_leakage.py pattern deliberately: every invariant is
tested twice, once on clean input (proves the feature is computed right) and
once on a deliberately corrupted input (proves the check can actually fail --
a checker that always passes proves nothing, see that file's docstring).

These tests do not re-verify A1's boundary itself (``truncate_history`` /
``check_no_leakage`` already have that coverage); they verify that A2's new
features -- built ON TOP of an already-truncated history -- don't reintroduce
a leak of their own, e.g. by looking up a click's timestamp from the full
history instead of the truncated one.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

A1_ROOT = Path(__file__).resolve().parents[2] / "Assignment-1-Lexical-Semantic-Retrieval"
sys.path.insert(0, str(A1_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.schema import Article, History, Impression  # noqa: E402
from a2src.rerank.features import (  # noqa: E402
    SessionTracker,
    build_candidate_rows,
    recency_weighted_history,
)
from a2src.rerank.candidates import (  # noqa: E402
    RetrievalCoverage,
    build_rerank_table,
    retrieve_candidates,
)


def _article(aid: str, category: str = "news", published: datetime | None = None) -> Article:
    return Article(article_id=aid, title=f"title {aid}", category=category, published_time=published)


def _imp(iid: str, user: str, when: datetime, candidates: list[str], clicked: list[str]) -> Impression:
    return Impression(impression_id=iid, user_id=user, time=when, candidates=candidates, clicked=clicked)


class TestRecencyDecayRespectsBoundary:
    def test_only_past_clicks_get_weight(self):
        """Clean case: weights come only from ids actually passed in."""
        t = datetime(2023, 5, 20, 12, 0)
        past_ids = ["a", "b"]  # caller already truncated -- "FUTURE" excluded
        times = {"a": t - timedelta(hours=1), "b": t - timedelta(hours=25)}
        weights = recency_weighted_history(past_ids, [times["a"], times["b"]], t)
        assert set(weights) == {"a", "b"}
        assert weights["a"] > weights["b"], "more recent click must weigh more"

    def test_a_future_timestamp_smuggled_in_is_dropped_not_leaked(self):
        """Mutation test: if a caller's truncation is broken and a future
        click slips into ``past_ids``, the decay function must not silently
        assign it a (very high, since age<0) weight -- it must be excluded.
        If this test starts failing, recency_weighted_history has started
        rewarding leaked future clicks instead of refusing them."""
        t = datetime(2023, 5, 20, 12, 0)
        corrupted_ids = ["past", "FUTURE"]
        times = [t - timedelta(hours=1), t + timedelta(hours=1)]
        weights = recency_weighted_history(corrupted_ids, times, t)
        assert "FUTURE" not in weights, "a future click received a recency weight"
        assert "past" in weights


class TestCandidateRowsRespectBoundary:
    def test_history_click_count_matches_truncated_history_only(self):
        """Clean case: the row's history_click_count is len(past_click_ids),
        not len(the user's full history)."""
        t = datetime(2023, 5, 20, 12, 0)
        articles = {"a": _article("a"), "b": _article("b"), "cand": _article("cand")}
        imp = _imp("i1", "u1", t, candidates=["cand"], clicked=[])
        past_click_ids = ["a"]  # already truncated by the caller; "b" excluded
        rows = build_candidate_rows(imp, past_click_ids, None, articles, {})
        assert rows[0].history_click_count == 1

    def test_feature_rows_are_insensitive_to_untruncated_history_leaking_in(self):
        """Mutation test: if a bug passed the FULL (untruncated) history into
        build_candidate_rows instead of the pre-boundary one, this test would
        catch the resulting inflated history_click_count. Simulates the bug
        by comparing truncated vs. untruncated call results directly."""
        t = datetime(2023, 5, 20, 12, 0)
        articles = {"a": _article("a"), "b": _article("b"), "cand": _article("cand")}
        imp = _imp("i1", "u1", t, candidates=["cand"], clicked=[])

        truncated = ["a"]  # correct: "b" (a future click) already excluded
        leaked = ["a", "b"]  # bug: future click reached this call

        rows_ok = build_candidate_rows(imp, truncated, None, articles, {})
        rows_bad = build_candidate_rows(imp, leaked, None, articles, {})
        assert rows_ok[0].history_click_count != rows_bad[0].history_click_count, (
            "history_click_count did not change when a leaked click was added -- "
            "the feature is not actually reading the history it was given"
        )
        assert rows_ok[0].history_click_count == 1

    def test_freshness_uses_impression_time_not_wall_clock(self):
        """freshness_hours must be relative to the IMPRESSION's time, so a
        feature table built for a past impression can't accidentally encode
        today's date."""
        published = datetime(2023, 5, 20, 10, 0)
        imp_time = datetime(2023, 5, 20, 12, 0)
        articles = {"cand": _article("cand", published=published)}
        imp = _imp("i1", "u1", imp_time, candidates=["cand"], clicked=[])
        rows = build_candidate_rows(imp, [], None, articles, {})
        assert rows[0].freshness_hours == 2.0

    def test_freshness_cannot_be_negative_for_a_clean_pipeline(self):
        """Invariant carried over from the teammate's notebook leakage check
        (Section 24): an article can't be 'fresh' relative to a time before it
        was published. A negative value here means the impression predates the
        article's publish time, which should not happen for real click data."""
        published = datetime(2023, 5, 20, 10, 0)
        imp_time = datetime(2023, 5, 20, 8, 0)  # BEFORE publish -- malformed input
        articles = {"cand": _article("cand", published=published)}
        imp = _imp("i1", "u1", imp_time, candidates=["cand"], clicked=[])
        rows = build_candidate_rows(imp, [], None, articles, {})
        assert rows[0].freshness_hours is not None and rows[0].freshness_hours < 0, (
            "this row IS the violation case -- assert_no_impossible_freshness "
            "(in scripts/build_rerank_table.py) is what should catch it on real data"
        )


class TestSessionTrackerRespectsBoundary:
    def test_second_impression_sees_first_impressions_click(self):
        """Clean case: session count accumulates only from impressions
        observed strictly before the query."""
        t = datetime(2023, 5, 20, 12, 0)
        imp1 = _imp("i1", "u1", t, candidates=["a"], clicked=["a"])
        imp2 = _imp("i2", "u1", t + timedelta(minutes=5), candidates=["b"], clicked=[])
        imp1.session_id = "s1"
        imp2.session_id = "s1"

        tracker = SessionTracker()
        assert tracker.click_count_before(imp1) == 0
        tracker.observe(imp1)
        assert tracker.click_count_before(imp2) == 1

    def test_an_impression_never_sees_its_own_click(self):
        """Mutation test: if observe() were (wrongly) called BEFORE
        click_count_before() for the SAME impression, that impression would
        see its own click as prior session activity -- a same-impression
        leak. This asserts the ordering that prevents it: querying an
        impression before it has been observed must not reflect its own
        clicks, no matter how many clicks it has."""
        t = datetime(2023, 5, 20, 12, 0)
        imp = _imp("i1", "u1", t, candidates=["a", "b"], clicked=["a", "b"])
        imp.session_id = "s1"

        tracker = SessionTracker()
        count_before_self_observe = tracker.click_count_before(imp)
        assert count_before_self_observe == 0, (
            "an impression's own clicks leaked into its own session_click_count"
        )

    def test_mind_has_no_sessions_and_returns_zero_honestly(self):
        """MIND's reader never sets session_id (readers.py) -- confirms the
        degradation is an honest 0, not a crash or a fabricated value."""
        t = datetime(2019, 11, 15, 12, 0)
        imp = _imp("i1", "u1", t, candidates=["a"], clicked=["a"])
        assert imp.session_id is None
        tracker = SessionTracker()
        assert tracker.click_count_before(imp) == 0
        tracker.observe(imp)  # must not raise on a None session_id
        assert tracker.click_count_before(imp) == 0


class TestClickedArticleSurvivesTruncation:
    """Regression test: retrieve_candidates() used to append the clicked
    article AFTER truncating to top_k, so a clicked article the retriever
    missed but that would have landed past position top_k got silently
    dropped anyway -- defeating the whole point of the append. Caught by
    dry-running Q3's ablation on real EB-NeRD data: 500 val impressions
    produced only 1 impression with its clicked article present."""

    class _StubRetriever:
        """Always returns top_k UNRELATED ids -- simulates a retriever that
        completely misses the true clicked article, the exact case the
        append exists to rescue."""

        def retrieve(self, history_text, k, at_time=None):
            return [(f"decoy{i}", 1.0 / (i + 1)) for i in range(k)]

        def score_subset(self, history_text, subset):
            return {aid: 0.0 for aid in subset}

    def test_missed_click_is_appended_not_dropped(self):
        imp = _imp(
            "i1", "u1", datetime(2023, 5, 20, 12, 0),
            candidates=["decoy0", "clicked_but_missed"],
            clicked=["clicked_but_missed"],
        )
        articles = {"a": _article("a")}  # only needs a truthy history entry
        candidate_ids, appended, _scores = retrieve_candidates(self._StubRetriever(), ["a"], articles, imp, top_k=5)
        assert "clicked_but_missed" in candidate_ids, (
            "the clicked article was dropped when the retriever's top_k was "
            "already full before the append -- see the docstring above"
        )
        assert appended == {"clicked_but_missed"}, (
            "the appended-ids set must mark which rows were NOT organically "
            "ranked, so callers never turn their position into a feature"
        )


class TestDisplayPositionIsNotALabelLeak:
    """Regression test for the AUC-0.0095-to-0.985 bug: display_position
    must reflect the REAL dataset slate order, and must be None/NaN for a
    candidate that was never in that real slate -- never the index into the
    retrieval-simulated candidate list, which correlates almost perfectly
    with clicked=1 under this pipeline's construction (every appended-on-miss
    row is a click)."""

    def test_display_position_comes_from_the_real_slate_not_retrieval_order(self):
        t = datetime(2023, 5, 20, 12, 0)
        articles = {"x": _article("x"), "y": _article("y"), "z": _article("z")}
        # The REAL displayed slate was [x, y, z] -- z at position 3.
        real_slate_positions = {"x": 1, "y": 2, "z": 3}
        # But the retrieval-simulated candidate list (what a caller might be
        # tempted to enumerate) puts z FIRST, because that's just where the
        # retriever happened to rank it.
        imp = _imp("i1", "u1", t, candidates=["z", "x", "y"], clicked=["z"])
        rows = build_candidate_rows(
            imp, [], None, articles, {}, display_position_by_article=real_slate_positions
        )
        by_id = {r.article_id: r for r in rows}
        assert by_id["z"].display_position == 3, (
            "display_position followed retrieval-list order (z is first there) "
            "instead of the real slate order (z is 3rd there) -- this IS the bug"
        )
        assert by_id["z"].clicked == 1

    def test_appended_candidate_gets_no_display_position(self):
        """A candidate absent from the real slate (i.e. it was appended
        because the retriever missed the click, or purely a retrieval hit)
        must get None, not a fabricated position -- there is no real
        position-bias signal for an article the user never actually saw
        in that position."""
        t = datetime(2023, 5, 20, 12, 0)
        articles = {"only_shown": _article("only_shown"), "retrieval_only": _article("retrieval_only")}
        real_slate_positions = {"only_shown": 1}  # "retrieval_only" was never displayed
        imp = _imp("i1", "u1", t, candidates=["only_shown", "retrieval_only"], clicked=["retrieval_only"])
        rows = build_candidate_rows(
            imp, [], None, articles, {}, display_position_by_article=real_slate_positions
        )
        by_id = {r.article_id: r for r in rows}
        assert by_id["retrieval_only"].display_position is None
        assert by_id["retrieval_only"].clicked == 1, (
            "the missing-position candidate is ALSO the clicked one here -- "
            "proving None is not itself acting as a disguised label signal "
            "would require checking feature importance on real data, done "
            "separately via the full run_a2.py dry run"
        )


class TestScoreSlateKeying:
    """Regression test for the Codabench submission bug: RerankedRetriever
    .score_slate() returned scores keyed by the raw (impression_id,
    article_id) tuple that reranker.score_reranker() itself uses internally
    -- but every caller (rank_candidates in predict_submission.py) looks
    scores up by bare article_id. Every lookup silently missed, defaulted to
    -inf, and the whole prediction file came out as trivial identity order
    [1,2,3,...,N] for 200/200 sampled impressions -- caught only by
    inspecting the actual generated prediction file, not by any earlier
    unit test, because nothing had exercised score_slate() until the
    submission script was written."""

    def test_scores_are_keyed_by_bare_article_id(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Assignment-1-Lexical-Semantic-Retrieval"))
        from a2src.rerank.reranked_retriever import RerankedRetriever
        from a2src.rerank.reranker import train_reranker

        t = datetime(2023, 5, 20, 12, 0)
        articles = {
            "a": _article("a", category="news"),
            "b": _article("b", category="sports"),
            "c": _article("c", category="news"),
        }
        # Build a tiny training table with SOME signal so LightGBM has
        # something to learn (category_match varies with clicked here).
        train_imps = [
            _imp("t1", "u1", t, candidates=["a", "b"], clicked=["a"]),
            _imp("t2", "u2", t, candidates=["a", "b"], clicked=["b"]),
        ]
        rows = []
        for imp in train_imps:
            rows.extend(build_candidate_rows(imp, [], None, articles, {"a": 0.9, "b": 0.1, "c": 0.5}))
        booster, feature_names = train_reranker(rows, num_boost_round=5)

        class _StubBase:
            name = "stub"
            def index(self, articles): pass
            def retrieve(self, history_text, k, at_time=None): return []

        reranked = RerankedRetriever(_StubBase(), booster, feature_names, articles, {"a": 0.9, "b": 0.1, "c": 0.5})
        imp = _imp("i1", "u1", t, candidates=["a", "b", "c"], clicked=[])
        scores = reranked.score_slate(imp, [])

        assert set(scores.keys()) == {"a", "b", "c"}, (
            f"score_slate keys must be bare article ids, got {set(scores.keys())} -- "
            "if these are (impression_id, article_id) tuples, every lookup by "
            "article id alone (e.g. rank_candidates) silently misses"
        )


class TestRetrievalScoreHasNoMissingnessLeak:
    """Regression test for the SECOND retrieval_score leak: appended
    candidates (the retriever missed the click, so candidates.py appends
    it) used to get NO score at all, so retrieval_score came out NaN for
    them -- and since every appended id is a click by construction, NaN
    became a near-perfect proxy for clicked=1. A submission-time re-ranker
    trained with article_popularity excluded (so retrieval_score dominated
    feature importance) showed offline AUC 0.99 and a REAL Codabench
    leaderboard score of 0.5124 -- below A1's own un-reranked plain-BM25
    leaderboard baseline of 0.5568 on the identical competition. Fixed by
    scoring appended candidates via retriever.score_subset instead of
    leaving them unscored."""

    class _StubRetriever:
        """Organic retrieval always misses the click; score_subset gives
        it a real (low) score instead of nothing."""

        def retrieve(self, history_text, k, at_time=None):
            return [(f"decoy{i}", 10.0 - i) for i in range(k)]

        def score_subset(self, history_text, subset):
            return {aid: -1.0 for aid in subset}  # a genuine, low score

    def test_appended_candidate_gets_a_real_score_not_none(self):
        imp = _imp(
            "i1", "u1", datetime(2023, 5, 20, 12, 0),
            candidates=["decoy0", "clicked_but_missed"],
            clicked=["clicked_but_missed"],
        )
        articles = {"a": _article("a")}
        candidate_ids, appended, scores = retrieve_candidates(
            self._StubRetriever(), ["a"], articles, imp, top_k=5
        )
        assert "clicked_but_missed" in appended
        assert "clicked_but_missed" in scores, (
            "an appended (missed) candidate has no retrieval_score at all -- "
            "this is the exact condition that made NaN a label proxy"
        )
        assert scores["clicked_but_missed"] == -1.0

    def test_missingness_does_not_correlate_with_appended_status(self):
        """Every id in the returned candidate list -- organic or appended
        -- must appear in scores. A caller building CandidateFeatures from
        a partial scores dict would silently reintroduce the leak even if
        this function itself is correct, so the invariant checked here is
        the one that actually protects against recurrence."""
        imp = _imp(
            "i1", "u1", datetime(2023, 5, 20, 12, 0),
            candidates=["decoy0", "missed_click"],
            clicked=["missed_click"],
        )
        articles = {"a": _article("a")}
        candidate_ids, appended, scores = retrieve_candidates(
            self._StubRetriever(), ["a"], articles, imp, top_k=5
        )
        assert set(candidate_ids) == set(scores.keys()), (
            f"candidate_ids={set(candidate_ids)} vs scored={set(scores.keys())} -- "
            "every candidate must be scored, none left as an implicit NaN"
        )


class TestRetrievalCoverage:
    """Regression coverage for recall@K/precision@K instrumentation. This
    pipeline never measured either before: the guaranteed append
    (retrieve_candidates) makes every TRAINING row look like a "hit" by
    construction -- recall has to be counted at retrieval time, before the
    append, or it silently reads as 100% no matter how bad the retriever is."""

    class _AlwaysMissRetriever:
        """Never organically finds the click -- every candidate is a decoy.
        Coverage must read as 0%, not 100%, even though every row in the
        resulting table still has the click (via the append)."""

        name = "always-miss"

        def retrieve(self, history_text, k, at_time=None):
            return [(f"decoy{i}", 1.0 / (i + 1)) for i in range(k)]

        def score_subset(self, history_text, subset):
            return {aid: 0.0 for aid in subset}

    class _AlwaysHitRetriever:
        """Always organically ranks the click first. Coverage must read as
        100%."""

        name = "always-hit"

        def __init__(self, click_id):
            self.click_id = click_id

        def retrieve(self, history_text, k, at_time=None):
            return [(self.click_id, 1.0)] + [(f"decoy{i}", 1.0 / (i + 2)) for i in range(k - 1)]

        def score_subset(self, history_text, subset):
            return {aid: (1.0 if aid == self.click_id else 0.0) for aid in subset}

    def test_a_retriever_that_always_misses_reads_zero_coverage(self):
        t = datetime(2023, 5, 20, 12, 0)
        articles = {"h": _article("h")}
        history = {"u1": History(user_id="u1", clicked_ids=["h"], times=[t - timedelta(hours=1)])}
        imps = [_imp("i1", "u1", t, candidates=["decoy0", "the_click"], clicked=["the_click"])]
        coverage = RetrievalCoverage()
        build_rerank_table(
            self._AlwaysMissRetriever(), imps, history, articles, {}, top_k=5, coverage=coverage
        )
        assert coverage.impressions_checked == 1
        assert coverage.impressions_with_click_retrieved == 0
        assert coverage.recall_at_k == 0.0, (
            "a retriever that never organically finds the click must show 0% "
            "recall, even though the row table itself still has the click "
            "via the guaranteed append -- coverage must be measured before "
            "that append, not read off the resulting table"
        )
        assert coverage.precision_at_k == 0.0

    def test_a_retriever_that_always_hits_reads_full_coverage(self):
        t = datetime(2023, 5, 20, 12, 0)
        articles = {"h": _article("h")}
        history = {"u1": History(user_id="u1", clicked_ids=["h"], times=[t - timedelta(hours=1)])}
        imps = [_imp("i1", "u1", t, candidates=["the_click", "decoy0"], clicked=["the_click"])]
        coverage = RetrievalCoverage()
        build_rerank_table(
            self._AlwaysHitRetriever("the_click"), imps, history, articles, {}, top_k=5,
            coverage=coverage,
        )
        assert coverage.impressions_checked == 1
        assert coverage.impressions_with_click_retrieved == 1
        assert coverage.recall_at_k == 1.0
        # single-click impression, one organic hit out of top_k=5 -> 1/5,
        # confirming precision@K = recall@K / K in the single-click regime
        # (see RetrievalCoverage's docstring)
        assert coverage.precision_at_k == 1.0 / 5
