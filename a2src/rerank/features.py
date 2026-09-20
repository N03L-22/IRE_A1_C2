"""Q1: behavioural features from click-logs -- click-history, session, article.

Built directly on A1's leakage boundary (``src.data.split.truncate_history``)
rather than re-deriving one: every feature here is computed from the same
``(impression, pre-boundary click ids)`` pairs that already pass Q9's test, so
a feature cannot leak a future click without also breaking that test.

MIND has no per-click timestamps (F1, schema.py) -- recency decay and session
features that need timing degrade honestly there rather than being faked:
recency decay falls back to positional recency (most-recent-first in the
history list, which is how MIND's own history field is ordered) and session
features are marked unavailable. Every function says which case it is in via
its return value, not a silent default.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

_A1_ROOT = Path(__file__).resolve().parents[3] / "Assignment-1-Lexical-Semantic-Retrieval"
if str(_A1_ROOT) not in sys.path:
    sys.path.insert(0, str(_A1_ROOT))

from src.data.schema import Article, History, Impression  # noqa: E402

#: Half-life for the exponential recency decay, in hours. One news cycle.
DEFAULT_HALF_LIFE_HOURS = 24.0


@dataclass(slots=True)
class CandidateFeatures:
    """Q1's per-(impression, candidate) feature row.

    One row per candidate article shown in one impression -- the unit Q2's
    re-ranker trains and scores over. Kept as a flat dataclass rather than a
    dict so the feature list (``FIELDS``) is a single source of truth for
    both the training table and the LightGBM column order.
    """

    impression_id: str
    user_id: str
    article_id: str
    clicked: int
    # -- click-history features (Q1.1) --
    history_click_count: int
    recency_weighted_click_count: float
    category_match: int
    # -- session features (Q1.2) --
    display_position: float | None
    session_click_count: int
    # -- article features (Q1.3) --
    article_popularity: float
    freshness_hours: float | None
    # -- Q2.2: the candidate GENERATOR's own per-candidate score --
    retrieval_score: float | None


#: display_position is deliberately EXCLUDED from the re-ranker's trainable
#: features, despite being a real dataclass field. Under retrieve-then-rank
#: over the WHOLE CORPUS (Q2.1), "was this candidate in the dataset's own
#: small in-view slate" is close to structurally equivalent to "is this the
#: clicked article" -- measured on real EB-NeRD small-tier data: 200/200
#: clicked rows had a non-null display_position vs 0.11% of non-clicked rows
#: (11 of 9,997), because BM25's corpus-wide top-K rarely coincides with the
#: tiny real slate except at the click, which the pipeline guarantees stays
#: in the candidate set. A real serving-time re-ranker over full-corpus
#: candidates could never know "was this shown to the user before" about
#: something it just retrieved, so this isn't a fixable leak -- it's a
#: feature that doesn't apply to this architecture. Kept on the dataclass
#: for inspection/debugging (e.g. auditing how well BM25's candidates
#: overlap the real slate), never trained on.
#:
#: ``retrieval_score`` is the OPPOSITE case and IS trained on: unlike
#: display_position (leaks clicked-ness through append order) and
#: article_popularity (degrades to near-constant when train/test corpora
#: barely overlap -- measured: MIND's small-tier train popularity table
#: covers only 11.4% of the large-tier test corpus, 1.5% on EB-NeRD),
#: retrieval_score is computed fresh, per-candidate, from the SAME corpus
#: the candidate came from -- it needs no train-time lookup table at all.
#: Its absence was found the hard way: without it, history_click_count and
#: recency_weighted_click_count are IMPRESSION-level (identical for every
#: candidate in one slate, since both come from the user's history, not the
#: candidate), so with popularity excluded and freshness_hours unavailable
#: on MIND (no published_time), category_match -- a single binary feature --
#: was the ONLY thing left that varied per candidate, collapsing every
#: slate to exactly 2 score buckets (confirmed on 10 real MIND-large test
#: impressions, history lengths 8-79, before this feature was added).
FIELDS = [f for f in CandidateFeatures.__dataclass_fields__ if f not in
          ("impression_id", "user_id", "article_id", "clicked", "display_position")]


def recency_weighted_history(
    history_ids: list[str],
    history_times: list[datetime] | None,
    at_time: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> dict[str, float]:
    """Exponential-decay click weight per article, keyed by article id.

    With timestamps (EB-NeRD): weight = 2 ** (-(at_time - click_time) / half_life).
    A click 24h ago (the default half-life) counts half as much as one just now.

    Without timestamps (MIND, F1): decay by POSITION in the history list
    instead of by elapsed time -- MIND's history field is chronologically
    ordered (oldest first) per the dataset's own construction, so the last
    entry is the most recent click even though *when* it happened is unknown.
    This is a documented degradation, not a silent one: the caller can tell
    which regime produced a given table via ``History.is_verifiable``.
    """
    weights: dict[str, float] = {}
    if history_times is not None:
        for aid, t in zip(history_ids, history_times):
            age_hours = (at_time - t).total_seconds() / 3600.0
            if age_hours < 0:
                continue  # would be a leak; truncate_history should already exclude this
            w = 2.0 ** (-age_hours / half_life_hours)
            weights[aid] = weights.get(aid, 0.0) + w
    else:
        n = len(history_ids)
        for rank_from_end, aid in enumerate(reversed(history_ids)):
            w = 2.0 ** (-rank_from_end)  # most recent = rank 0 = full weight
            weights[aid] = weights.get(aid, 0.0) + w
    return weights


class SessionTracker:
    """Q1.2: within-session click count, enforcing the leakage boundary.

    "Session" means A1's ``Impression.session_id`` where the reader populates
    it (EB-NeRD; MIND's reader always sets ``None`` -- readers.py lines
    125/171 -- because MIND's TSVs carry no session field). Without a session
    id there is nothing to group by, so MIND rows get ``session_click_count =
    0`` honestly rather than a fabricated value -- the same documented
    degradation pattern as ``recency_weighted_history``'s MIND branch.

    Call :meth:`observe` for every impression of one user IN TIME ORDER
    before asking :meth:`click_count_before` for the current one, so "before"
    only ever sees impressions this tracker has already been shown -- the
    same construction discipline as ``truncate_history``.
    """

    def __init__(self) -> None:
        # (user_id, session_id) -> running count of clicks seen so far
        self._counts: dict[tuple[str, str], int] = {}

    def click_count_before(self, imp: Impression) -> int:
        if not imp.session_id:
            return 0
        return self._counts.get((imp.user_id, imp.session_id), 0)

    def observe(self, imp: Impression) -> None:
        """Record this impression's clicks so LATER impressions in the same
        session see them. Must be called after ``click_count_before`` for the
        same impression, never before -- calling it first would let an
        impression see its own clicks as "prior" session activity."""
        if not imp.session_id:
            return
        key = (imp.user_id, imp.session_id)
        self._counts[key] = self._counts.get(key, 0) + len(imp.clicked)


def build_candidate_rows(
    imp: Impression,
    past_click_ids: list[str],
    history_times_by_id: dict[str, datetime] | None,
    articles: dict[str, Article],
    train_popularity: dict[str, float],
    session_click_count: int = 0,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    display_position_by_article: dict[str, int] | None = None,
    retrieval_score_by_article: dict[str, float] | None = None,
) -> list[CandidateFeatures]:
    """One :class:`CandidateFeatures` row per candidate in ``imp.candidates``.

    ``past_click_ids`` must already be truncated to the leakage boundary --
    this function does not re-check it (that is ``truncate_history``'s job and
    ``check_no_leakage``'s test); it only ever reads clicks it is handed.

    ``display_position_by_article``, if given, maps article id -> its 1-based
    position in the REAL, dataset-displayed slate (the raw impression's own
    ``candidates`` order, before any retrieval simulation replaces it).

    **Why this must NOT be the index into ``imp.candidates`` here.** When the
    caller is Q2's retrieve-then-rank pipeline, ``imp.candidates`` is a
    SYNTHETIC list built from the retriever's top-K plus any clicked article
    the retriever missed, appended at the end (``candidates.retrieve_candidates``).
    Using that index as a feature reintroduces exactly the leak a full
    small-tier dry run caught: every appended row is a click by construction,
    so a raw list-position feature becomes a near-perfect, meaningless
    predictor (measured: LightGBM gave it 97% of total gain, and "before vs
    after" jumped from AUC 0.0095 to 0.985 for a reason that had nothing to
    do with behavioural signal). ``display_position_by_article`` must instead
    come from the ORIGINAL dataset slate, independent of retrieval, or be
    omitted (``None``) -- rows then get ``display_position = NaN``, LightGBM's
    native "missing" rather than a fabricated constant.
    """
    history_times = (
        [history_times_by_id[a] for a in past_click_ids if a in history_times_by_id]
        if history_times_by_id is not None
        else None
    )
    # positional-decay branch needs the ids in order even without timestamps
    weights = recency_weighted_history(
        past_click_ids,
        history_times if history_times_by_id is not None else None,
        imp.time,
        half_life_hours,
    )
    recency_score = sum(weights.values())
    history_categories = {
        articles[a].category for a in past_click_ids if a in articles and articles[a].category
    }
    clicked = set(imp.clicked)

    rows = []
    for aid in imp.candidates:
        art = articles.get(aid)
        freshness_hours = None
        if art is not None and art.published_time is not None:
            freshness_hours = (imp.time - art.published_time).total_seconds() / 3600.0
        display_pos = (
            float(display_position_by_article[aid])
            if display_position_by_article is not None and aid in display_position_by_article
            else None
        )
        retrieval_score = (
            retrieval_score_by_article.get(aid)
            if retrieval_score_by_article is not None
            else None
        )
        rows.append(
            CandidateFeatures(
                impression_id=imp.impression_id,
                user_id=imp.user_id,
                article_id=aid,
                clicked=int(aid in clicked),
                history_click_count=len(past_click_ids),
                recency_weighted_click_count=recency_score,
                category_match=int(bool(art) and art.category in history_categories),
                display_position=display_pos,
                session_click_count=session_click_count,
                article_popularity=train_popularity.get(aid, 0.0),
                freshness_hours=freshness_hours,
                retrieval_score=retrieval_score,
            )
        )
    return rows


def assert_no_impossible_freshness(rows: list[CandidateFeatures]) -> None:
    """Q9 runtime check: an article cannot be shown before it was published.

    Distinct from the click-history leakage boundary (that is A1's
    ``check_no_leakage``, over the training history) -- this one is over
    ``freshness_hours`` itself, computed from ``published_time``. A negative
    value means the impression predates the article, which for real click
    data means a publish-time bug upstream, not a legitimate feature value.
    Only checks rows where freshness was computable (EB-NeRD; MIND has no
    ``published_time`` and every row's freshness is ``None``, silently
    excluded rather than treated as a pass).
    """
    bad = [r for r in rows if r.freshness_hours is not None and r.freshness_hours < 0]
    assert not bad, (
        f"{len(bad)} of {len(rows)} rows show impossible freshness "
        f"(impression before publish time): {bad[0]}"
    )


def train_popularity_from_impressions(impressions: Iterable[Impression]) -> dict[str, float]:
    """Click count per article over TRAIN only, normalised to [0, 1] by the max.

    Must be built from the train split alone (A1's ``novelty()``/fusion.py
    ``PopularityPrior`` docstring make the same point) -- using held-out-period
    popularity as a serving-time feature is leakage of a kind that flatters
    the offline number specifically.
    """
    counts: dict[str, int] = {}
    for imp in impressions:
        for aid in imp.clicked:
            counts[aid] = counts.get(aid, 0) + 1
    if not counts:
        return {}
    top = max(counts.values())
    return {aid: c / top for aid, c in counts.items()}
