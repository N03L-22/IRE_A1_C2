---
type: assignment
title: Assignment 2 — Learning from Click-Logs on EB-NeRD and MIND
status: in-progress
due: 2026-09-20
priority: high
---

# Assignment 2 — Learning from Click-Logs

CS4.406, teams of 2. Builds on [[../Assignment-1-Lexical-Semantic-Retrieval/Assignment-1-Lexical-Semantic-Retrieval|Assignment 1]]'s
retrieve pipeline: add behavioural signals, train a re-ranker, reproduce-then-beat a baseline with
an ablation, and analyse serving/scale — on **both** EB-NeRD and MIND.

📄 Brief: `brief/A2.pdf`

## Context

A teammate built a first pass as a single Kaggle notebook: `reference/assignment2_kaggle.ipynb`.
Review found it well-designed in places (real, non-fabricated Q4 serving measurements; a correct
paired-bootstrap Q3 ablation) but with two blocking issues:
- **Never executed** — zero cell outputs, zero execution counts.
- **EB-NeRD only past Q1** — Section 22-24 (behavioural features) covers both datasets, but the
  re-ranker, NRMS-lite baseline, ablation, serving benchmark, and extended eval (Sections 25-35)
  only ever touch EB-NeRD, despite the brief requiring both throughout. The wrap-up cell claims a
  MIND submission that doesn't exist in the notebook.
- Also flagged: `predict_final.py`'s test-time behavioural features are hardcoded placeholders
  (zeros/nulls), and the original notebook this was rebuilt from apparently had a hardcoded HF API
  token — confirm it's revoked before anything is pushed to GitHub Classroom.

Decision: rebuild properly in `src/`, reusing [[../Assignment-1-Lexical-Semantic-Retrieval/architecture|Assignment 1's]]
mature `Retriever` interface, leakage boundary, and eval harness (which already includes a paired
bootstrap CI function) rather than the notebook's from-scratch polars reimplementation. The
notebook stays as a reference/comparison point, not the deliverable.

## Submissions

- [x] Q1 — click-history/session/article features + leakage-boundary test, both datasets ✅ 2026-09-18
- [x] Q2 — two-stage retrieve-then-rank (LightGBM re-ranker over A1's BM25 candidates), both datasets ✅ 2026-09-18
- [x] Q3 — NRMS-lite baseline reproduced, one improvement ablated, paired bootstrap CI ✅ 2026-09-18
- [x] Q4 — serving/scale benchmark (index memory, p50/p90/p99 latency, cost/QPS) ✅ 2026-09-18
- [x] Q5 — extended eval wired through A1's harness (all metrics, cold-warm + head-tail slices, CIs), both datasets ✅ 2026-09-18
- [x] Scripted one-command rebuild (`scripts/run_a2.py --dataset {mind,ebnerd}`) — runs Q2-Q5 end to end, writes `results/a2_<dataset>.json` ✅ 2026-09-18
- [x] Small-tier rerun (uncapped, not demo-scale) for both datasets → `results/a2_ebnerd.json`, `results/a2_mind.json` ✅ 2026-09-18
- [x] Scaled-up small-tier rerun (60K train-period / 8K heldout impressions, ~4.5M candidate rows each dataset — 7.5× the earlier row count) confirms the fix holds at scale, not just at demo size ✅ 2026-09-18
- [x] Prediction file generation script (`scripts/predict_submission.py`, reuses A1's exact Codabench format) ✅ 2026-09-18
- [ ] Full-scale submission files (running in background at time of writing — `submissions/{mind,ebnerd}_a2_rerank_bm25_prediction.zip`) — upload + screenshots are a manual step outside this pipeline (account-bound) 📅 2026-09-20
- [x] Design note: large working doc (`report/design_note_full.md`) + 6-page-target LaTeX PDF (`report/design_note.tex`/`.pdf`, thin margins, 2 TikZ flow diagrams, currently 2 of 6 pages) ✅ 2026-09-18
- [ ] ai-log curated (`/ai-log`) 📅 2026-09-20
- [x] Added semantic retrieval + RRF fusion as the Q2.1 candidate generator — `scripts/run_a2.py --candidate-generator {bm25,bm25+semantic}` (default: fused) ✅ 2026-09-18

## Notes

- Data already available locally from A1's work: `../Assignment-1-Lexical-Semantic-Retrieval/data/{raw,work,store}/{mind,ebnerd}/` — no re-download needed for demo/small-scale dev.
- A2 has its OWN `.venv` (`--system-site-packages`, mirroring A1's setup) with `lightgbm` added — A1's environment was deliberately left untouched rather than installing a GBDT dependency it never needed. `requirements.txt` documents the pins.
- A1's `src/` is imported via `sys.path` from A2's `a2src/` (never copied) — A2's top-level package is named `a2src`, not `src`, specifically to avoid colliding with A1's own `src` package when both are on `sys.path` at once.

### Bugs found and fixed while building (not in the teammate's notebook, found during local dry-runs)
- **`session_click_count` was a stub always returning 0.** Fixed with `features.SessionTracker`, which counts prior clicks in the same `(user, session)` before the current impression — real nonzero values now appear on ~45% of EB-NeRD rows (MIND has no `session_id` in the reader, stays honestly 0). Covered by leakage-ordering tests in `tests/test_no_future_leak.py`.
- **Candidate truncation dropped the clicked article.** `candidates.retrieve_candidates` appended the impression's clicked id(s) *after* truncating retrieved results to `top_k` — if the retriever's own top-K was already full, the append never took effect. Found by dry-running Q3's ablation: 500 EB-NeRD val impressions produced only 1 with its clicked article present, making the paired bootstrap CI meaningless (n=1). Fixed by truncating first, then appending; after the fix, 500/500 EB-NeRD and 114/1000 MIND val impressions pair up correctly. Regression test added.
- **Two label-leak bugs via position features, found via a real small-tier run (not caught by unit tests alone — needed the full pipeline on real data):**
  1. `position_in_slate` was the literal index into the synthetic retrieval-order candidate list. Since the guaranteed-append (previous bug's fix) always puts a missed click at the END of that list, position became a near-perfect predictor of `clicked=1`. Measured: 97% of LightGBM's total gain, before/after AUC jumping from 0.0095 to 0.985 for a reason unrelated to behavioural signal.
  2. Renamed to `display_position` and redefined as the article's position in the dataset's REAL displayed slate (not the retrieval-simulated candidate list) — but this reintroduced the same class of leak one level up: EB-NeRD's real in-view slate is small (5–25 items) and always contains the click, while BM25's corpus-wide top-K candidates rarely coincide with it — so 200/200 clicked training rows got a real position value vs 0.11% of non-clicked rows (11 of 9,997). Presence-of-a-position became the leak, not its value.
  - **Fix**: `display_position` is excluded from the re-ranker's trainable feature set entirely (`FIELDS` in `features.py`), with the reasoning documented in code — a full-corpus retrieve-then-rank design has no legitimate way to know "was this shown before" about a candidate it just retrieved, so this isn't patchable, it's a feature that doesn't apply to this architecture. Kept on the dataclass only for inspection. After the fix: feature importance is `article_popularity` (82% EB-NeRD / 97% MIND), `freshness_hours`, `recency_weighted_click_count`, `history_click_count`, `session_click_count`, `category_match` — no single feature above ~98%, and before/after AUC is a believable 0.0095 → 0.816 (EB-NeRD) / 0.0 → 0.293 (MIND), not saturated at ~1.0. Two regression tests added (`TestDisplayPositionIsNotALabelLeak`).
  - **Lesson**: neither bug was visible from code review or synthetic unit tests — both only showed up as an implausibly perfect metric on a REAL small-tier run. Any future feature added to this pipeline should get the same treatment: run it, check feature_importance, and be suspicious of anything above ~30–40% of total gain.
- **Q4 memory measurement caveat**: `bench.measure_build_memory`'s RSS-delta approach (reused from the teammate's notebook design) is only accurate for the FIRST index built in a process — dry-run showed EB-NeRD's BM25 index memory as 0.0 MB when measured right after MIND's larger index in the same process, because the allocator had already grown the process's RSS and didn't need to grow it again. The real per-dataset scripts must measure each dataset's index in its own fresh process (or subtract a baseline), not sequentially in one script.
- **Q4 MIND latency**: p99 (11ms) is far above p50 (0.2ms) in the small-tier run — likely a cold-cache/first-call effect not fully absorbed by the 10-sample warmup at `--val-limit 800`. Worth widening the warmup or checking for outlier calls before quoting p99 in the design note.

### Results on file (small tier, both datasets — 2026-09-18)
`results/a2_{ebnerd,mind}.json` (3K train-period / 800 heldout impressions) and `results/a2_{ebnerd,mind}_large.json` (60K train-period / 8K heldout impressions, ~4.5M candidate rows each — the "full-scale" rerun; still small TIER, per the decision to stay within A1's own small-tier precedent rather than extract MIND-large train/dev, which A1 itself never does for offline metrics). Each JSON has: BM25 index memory, rerank train/val row counts, LightGBM feature importance, Q2 before/after (AUC/MRR/nDCG@5/@10), Q3 NRMS-lite training loss + val nDCG@10, Q3 ablation with paired bootstrap 95% CI, Q4 p50/p90/p99 latency + cost/QPS, and Q5's full extended-eval table (all metrics × cold/warm × head/tail slices × bootstrap CIs) via A1's unmodified harness.

**Scaled-up numbers held stable (7.5× the row count of the first real run) — the fix generalises, not a small-sample artifact:**
| | EB-NeRD (3K/800) | EB-NeRD (60K/8K) | MIND (3K/800) | MIND (60K/8K) |
|---|---|---|---|---|
| top feature | article_popularity 82% | article_popularity 82% | article_popularity 97% | article_popularity 98% |
| before→after AUC | 0.0095→0.816 | 0.0080→0.826 | 0.0→0.293 | 0.011→0.431 |
| before→after nDCG@10 | 0→0.062 | 0.00005→0.038 | 0→0.104 | 0.001→0.163 |
| ablation n (paired) | 683 | 6,560 | 88 | 927 |
| ablation significant? | no | no | no | no |
| Q4 p99 latency | 4.5ms | 4.7ms | 11.0ms | 5.8ms |

The recency/history-count ablation stays non-significant at every scale tested — consistent with [[../Assignment-1-Lexical-Semantic-Retrieval/Assignment-1-Lexical-Semantic-Retrieval|A1's]] recorded finding that a null offline result on this kind of broad feature change is often *unmeasured*, not *no effect* (the offline harness disagreed with A1's leaderboard 4/4 times); this needs the design note to say so explicitly rather than reporting "no improvement" flatly, and ideally a different ablation split (e.g. drop category_match or freshness_hours, which carry more of the gain on EB-NeRD) before concluding the recency features specifically don't help.

### Semantic + RRF fusion candidate generator (2026-09-18)
Added `--candidate-generator {bm25,bm25+semantic}` (default: fused) — "Assignment 1's candidate generator" means the whole stack it built (BM25 + MiniLM semantic + RRF fusion), not just BM25. Semantic uses A1's already-cached MiniLM embeddings (`encode_cached` with `--embed-cache-dir` pointed at A1's `data/store/embeddings/`) via the `vectors=`/`vector_ids=` path, so no re-encoding. Ran at the same 60K/8K scale as the BM25-only comparison:

| | EB-NeRD BM25-only | EB-NeRD fused | MIND BM25-only | MIND fused |
|---|---|---|---|---|
| top feature | popularity 82% | popularity 84% | popularity 98% | popularity 98% |
| before→after AUC | 0.008→0.826 | 0.015→0.841 | 0.011→0.431 | 0.020→0.433 |
| before→after nDCG@10 | 0.00005→0.038 | 0.0003→0.041 | 0.001→0.163 | 0.002→0.152 |
| ablation n (paired) | 6,560 | 6,560 | 927 | 927 |
| ablation significant? | no | no | no | **yes** (diff −0.0031, CI [−0.0056, −0.0007]) |
| Q4 p99 latency | 4.7ms | 21.1ms | 5.8ms | 11.2ms |
| candidate-gen index memory | ~93MB | 192MB | ~403MB | 702MB |

**MIND's ablation flips to significant under fusion** — but the sign is negative (dropping recency/history-count features *increased* nDCG@10 by 0.0031), i.e. under this candidate set those two features very slightly HURT the re-ranker on MIND, not helped. Worth restating carefully in the design note: this is evidence the recency/history-count pair isn't pulling its weight on MIND specifically (article_popularity dominates at 98% either way), not evidence they matter more under fusion. EB-NeRD's ablation stays non-significant under both candidate generators.

**Fusion trades real latency for a small accuracy gain** — worth stating explicitly as the trade-off in Q4/Q6 of the design note: ~4.5× (EB-NeRD) to ~2× (MIND) p99 latency increase for a modest AUC/nDCG lift on EB-NeRD and roughly a wash on MIND. The brute-force semantic `score_subset` call inside `_before_after_eval` (one per validation impression) is the dominant cost — a real serving system would batch or use an ANN index rather than exact brute-force at this corpus size, which A1's own `SemanticRetriever` already supports (`index_kind="auto"` switches to HNSW past a size threshold) but this comparison used brute-force throughout for a fair candidate-generator-only comparison.

Results on file: `results/a2_{ebnerd,mind}_fused.json`, alongside the earlier `_large.json` (BM25-only) files for direct comparison.

### Codabench submission pipeline (2026-09-18)
`scripts/predict_submission.py` generates the actual leaderboard prediction files, reusing A1's exact on-disk format (`src/submit/codabench.py`'s line format `impression_id [rank1,...,rankN]` and archive member names — `prediction.txt` for MIND, `predictions.txt` for EB-NeRD, one letter apart, and A1's own submission history records a rejected upload from getting this wrong).

Two bugs found only by generating and inspecting real prediction files:
- **Key-format mismatch**: `RerankedRetriever.score_slate` returned scores keyed by `(impression_id, article_id)` (matching `score_reranker`'s native output), but the rank-computing function expects bare `article_id`. Every lookup missed, every score defaulted to `-inf`, and 200/200 sampled prediction lines came out as the trivial identity permutation `[1,2,...,N]` — a submission that would have uploaded successfully and scored at chance. Fixed by flattening the key inside `score_slate`; regression test `TestScoreSlateKeying` added.
- **Discrimination collapse**: after the key fix, still 38–48/200 identity lines. `recency_weighted_click_count`/`history_click_count` are impression-level (same for every candidate in a slate), `display_position` is correctly excluded (see above), and `freshness_hours` is unavailable on MIND — leaving `category_match` (one binary feature) as the only per-candidate signal, collapsing every slate to 2 score buckets. Raising `--train-limit` didn't help: even the full small-tier train split covers only 11.4% (MIND) / 1.5% (EB-NeRD) of the large-tier test corpus's articles — small-tier train and large-tier test are largely disjoint article universes, not just under-sampled. Fixed by adding a new Q1 feature, `retrieval_score` (the candidate generator's own per-candidate score, needs no train-corpus lookup), and excluding `article_popularity` from the submission-time model specifically (it stays in for the offline `run_a2.py` eval, where train/val are matched-tier). Identity rate dropped to 15/200 after both fixes; sampled slates went from 1–2 unique scores to 3–21.

**A third bug, found running the full-scale job**: EB-NeRD's run crashed with `FileNotFoundError` on the test split — *after* the ~7-minute re-ranker training step had already completed, wasting it. Root cause: MIND's reader is tier-agnostic (`work_dir/mind/large_test` regardless of `--test-tier`), but EB-NeRD's reader nests by tier (`work_dir/ebnerd/{tier}/test`), and EB-NeRD's unlabelled test set was extracted from a *separate* archive (`ebnerd_testset.zip`) into its own `testset` tier — `large` only has train/validation there. A single shared `--test-tier=large` default worked for MIND by coincidence and silently failed for EB-NeRD. Fixed two ways: (1) `DEFAULT_TEST_TIER` is now a per-dataset dict (`mind→large`, `ebnerd→testset`); (2) added a cheap existence probe *before* the expensive training step, matching each reader's own path-construction logic, so a bad tier fails in milliseconds instead of after training.

Full-scale submission generation (train re-ranker on the complete small-tier split — 141,265 MIND / 209,597 EB-NeRD train impressions — predict on the real large-tier test set) was launched in the background; check `submissions/{mind,ebnerd}_a2_rerank_bm25_prediction.zip` for the finished files before uploading. Upload itself and leaderboard screenshots are a manual step outside this pipeline (Codabench submission is account-bound).

### A fourth bug, found via the REAL leaderboard score (2026-09-19)
The MIND submission (`mind_a2_rerank_bm25_prediction.zip`) uploaded and scored **AUC 0.5124** — below A1's own logged plain-BM25 leaderboard baseline (0.5568, `Assignment-1-.../submissions/mind_bm25_prediction.meta.json`) and well below A1's fusion best (0.6047). Since the assignment explicitly builds on A1, checking the new submission against A1's own submission history (not just against this pipeline's own offline numbers) is exactly the right comparison — and it caught a real regression the offline evaluation had missed entirely.

**Root cause: `retrieval_score` (added earlier to fix the display_position-exclusion discrimination collapse) reintroduced the SAME leak class through missingness instead of position.** Appended candidates (the retriever missed the click, so `candidates.py` appends it — see the earlier append-truncation bug) were left with `retrieval_score = None`/NaN, because the retriever never organically scored them. Since every appended id is a click by construction, NaN became a near-perfect proxy for `clicked=1` all over again. A submission-time model (`article_popularity` excluded, so `retrieval_score` dominated at 98%+ of gain) scored an implausible **offline AUC 0.99** — the same saturation signature as both earlier leaks — while the real leaderboard score was mediocre. Confirmed directly: re-ranker output vs. raw `retrieval_score` order had mean Spearman ρ ≈ **0.37** across sampled impressions (should be ≈1.0 if the model were just reproducing BM25 order, near 0 if genuinely uncorrelated) — the model was scrambling order in a way that tracked the leak artifact, not real relevance.

**Fix**: appended candidates are now scored via `retriever.score_subset` (the same slate-scoring primitive `score_slate`/`_before_after_eval` already use) instead of being left unscored — every candidate in the returned set gets a genuine retrieval score, so NaN never appears except for legitimate cold-start impressions (0.77% of rows, verified). After the fix: offline AUC 0.33→0.79 (believable, non-saturated), Spearman ρ between re-ranker and raw retrieval_score ≈ **-0.02** (genuinely independent, not fighting or copying BM25's order). Two regression tests added (`TestRetrievalScoreHasNoMissingnessLeak`).

**This is now the third instance of the same bug class in one pipeline** (position-in-slate leak → display-position leak → retrieval-score-missingness leak), each caught by a different signal: feature_importance saturation, a real prediction-file inspection, and finally a real leaderboard score falling below a known baseline. The common thread: any feature whose *presence/absence* — not just its *value* — correlates with how a candidate entered the candidate set (organic retrieval vs. appended-because-missed) is a leak risk in this retrieve-then-rank design, regardless of what the feature is nominally measuring.

**Submission files were regenerated after this fix** — the two `.zip` files referenced above (and their leaderboard scores, once re-submitted) should be treated as superseding the first MIND upload (score 0.5124, submission id 932585 on Codabench) rather than the current result.

### The real cause, found after checking A1's history again: train/serve mismatch, not a leak (2026-09-19)
Fixing the missingness leak (above) did NOT fix the leaderboard gap — the regenerated submission scored 0.5081, no better. Investigating further (comparing the re-ranker's output against plain BM25 order on real held-out labels) found a second, separate problem:

**The re-ranker was trained on a different, easier "test" than the one it actually faces at submission time.** During training, candidates for each impression came from asking BM25 to retrieve its top 150 out of MIND's entire ~65,000-article catalog. At real submission time, the model instead has to rank only the ~36 articles Codabench actually shows the user in that impression. Training on "pick the right one out of 150" and then grading on "pick the right one out of 36" are different tasks — the model's learned thresholds don't transfer. This is why the offline validation number looked fine the whole time: the offline check used the *same* 150-candidate construction as training, so it could never have noticed the mismatch — only the real leaderboard, which uses the real 36-candidate slate, could catch it.

**Confirmed directly**: scoring the real ~36-candidate slate with plain, un-reranked BM25 gives an offline AUC of 0.559 — almost exactly A1's real leaderboard score for plain BM25 (0.5568). Scoring the old 150-candidate training construction with plain BM25 gave 0.32, nowhere close. That gap between 0.32 and 0.559 is the whole story: the "before" number the offline evaluation reported the whole session was never a fair comparison to the leaderboard, because it was answering a different, harder question than the one Codabench actually asks.

**Fix**: added `build_rerank_table_on_slate`, which trains the re-ranker on the exact same ~36-candidate real slate it will be scored against, instead of a 150-candidate simulation. `run_a2.py`'s own offline Q2/Q3/Q5 evaluation is untouched (it genuinely needs the 150-candidate retrieve-then-rank version, since that experiment is *about* retrieval, not about matching Codabench's format) — only the submission path changed.

### Four MIND leaderboard iterations, and what each one taught us
| Version | What changed | Score |
|---|---|---|
| v1 | first attempt | 0.5124 |
| v2 | fixed the missingness leak (above) | 0.5081 (no better — wrong fix for this symptom) |
| v3 | + trained on the real slate, + added `display_position` (a candidate's position in the real slate) | 0.5187 |
| **v4** | same as v3, but **without** `display_position` | **0.5320 (best so far)** |
| v5 | same as v4 but candidates come from BM25+semantic fusion instead of BM25 alone | 0.5112 (worse than v4) |

`display_position` looked borderline-helpful offline but v3 vs v4 shows it was net-harmful on the real leaderboard — removing it is a real, measured +0.0133 gain. Still short of A1's own plain-BM25 leaderboard score (0.5568) by 0.0248 — real progress from v1 (was 0.0444 short), not yet fully closed.

### Why v5 (our fusion) scored *worse* than plain BM25, when A1's own fusion scored *much better*
This looks like a contradiction — same two retrievers (BM25 + semantic), combined the same way (reciprocal rank fusion) — so why would one version help a lot (A1: 0.5568 → 0.6047) and the other hurt (ours: BM25-alone 0.5320 → fusion 0.5112)? Checking A1's own saved submission record for its best fusion run answered it: **A1 didn't use the semantic retriever's default settings — it tuned two knobs specifically for MIND before submitting** (`tau=0.2`, `decay="flat"`, versus the library's defaults of `tau=0.35`, `decay="log"`). Our v5 used the untouched defaults.

In plain terms: the semantic side of the fusion decides how to summarize "what this user tends to click" into one search query, and there's more than one reasonable way to do that summary. A1 tried a few ways and kept whichever worked best for MIND specifically. We used whichever came out of the box, never checked whether it was a good fit for MIND, and fed that into the re-ranker on top. **The fusion candidate generator itself is very likely not equivalent to A1's, and the difference in leaderboard score reflects that untuned gap — not the re-ranker doing something wrong, and not evidence that fusion is a bad idea here.**

**Tested directly — v6, using A1's exact tuned values (`tau=0.2, decay=flat`) instead of the library defaults:** score **0.5309**, essentially tied with v4's BM25-only 0.5320 (difference within noise) and a large jump up from v5's untuned 0.5112. This confirms the hypothesis: the untuned semantic settings really were what made v5 look bad. But it also gives a real, informative negative result — **even correctly tuned, fusion candidates give the re-ranker no edge over plain BM25 candidates.** A1's fusion advantage (0.5568→0.6047, a raw-retrieval-only comparison, no re-ranker involved) does not carry over once a re-ranker sits on top of the candidates: either the re-ranker's own features already capture whatever fusion would have added, or training on the real displayed slate washes out the retrieval-order signal that made fusion valuable for A1 in the first place. BM25-only stays the better-performing candidate generator for this re-ranker, and v4 (0.5320) remains the best MIND result overall.

### Full MIND leaderboard progression
| Version | Config | Score |
|---|---|---|
| v1 | corpus-wide training | 0.5124 |
| v2 | + retrieval_score leak fix | 0.5081 |
| v3 | slate-trained + display_position, BM25 | 0.5187 |
| v4 | slate-trained, no display_position, BM25, K=150 | 0.5320 |
| v5 | slate-trained, no display_position, fusion (untuned), K=150 | 0.5112 |
| v6 | slate-trained, no display_position, fusion (A1-tuned tau=0.2/flat), K=150 | 0.5309 |
| **v7** | same as v4 but **K=200** (`mind_a2_rerank_bm25_k200_nopos_pred.zip`, Codabench id 934475) | **0.5346 (best, confirmed 2026-09-20)** |

v7 is the K=200 re-run of v4's exact winning config (plain BM25, no display_position) — the only variable changed was K, per the metrics-gap review above (Q2.1's own stated range is "K ~ 100-200"; there was never a reason to sit at the bottom of it). **+0.0026 over v4, the first real gain since v4 itself**, closing the gap to A1's own plain-BM25 leaderboard baseline (0.5568) from 0.0248 to 0.0222. Still short of it, and still well short of A1's fusion best — the re-ranker has closed most of the gap from v1 but has not yet demonstrably beaten A1's simplest baseline. Worth stating plainly in the design note rather than rounding up: Q3 asks to "reproduce a baseline then beat it," and as of v7 that has not happened on MIND's real leaderboard, even though the offline (slate-based) evaluation looks reasonable. This is itself a finding — see the train/serve-mismatch discussion above for why the offline number cannot yet be fully trusted as a predictor of the leaderboard result, and treat any further offline improvement as provisional until re-checked against Codabench.

(A leaderboard row `mind_fusion_5ed379_i1_prediction.zip`, score 0.5935, appears in the same competition but is **A1's own submission**, not this pipeline's — confirmed by checking its meta.json, which lives in `Assignment-1-.../submissions/`, not here. It is A1's raw fusion retriever with custom component weights, no re-ranker involved, and does not affect any A2 conclusion above.)

### A sixth bug, found by asking "why is A2 still below A1's own plain-BM25 baseline at all" (2026-09-20)

After v7 (0.5346), the gap to A1's plain-BM25 score (0.5568) was down to 0.0222 but still open, and every previously-documented cause (leaks, train/serve candidate-set mismatch, K) had already been fixed or tuned. Investigated whether some further, undocumented difference between A1's plain-BM25 submission path and A2's re-ranked-BM25 submission path could explain the remainder — both should, in principle, be scoring the same BM25-ranked candidates on the same real MIND test slates.

**Found: `predict_submission.py`'s BM25 index was never rebuilt on the real test corpus.** `train_reranker_for_submission` builds the candidate-generator's BM25 index from `--tier` (small, 65,238 MIND articles) so it can build training rows. That same `BM25Retriever` object was then reused unchanged all the way through to scoring the real test set — `main()` swapped `reranked.articles` (the plain dict used for category/freshness/popularity feature lookups) to the large-tier test corpus (120,961 articles), but never touched `reranked.base` (the actual BM25 index `score_slate` → `base.score_subset` calls). `BM25Retriever.score_subset`'s own docstring states plainly: "candidates absent from the index are simply missing from it, and the caller decides how to rank the unscored remainder." Since the small-tier train corpus covers only **11.4%** of MIND-large's test corpus (the same coverage number already used to justify excluding `article_popularity` — nobody had connected it to `retrieval_score`/BM25 itself), roughly **46% of every real test slate's candidates** could never be scored by BM25 at submission time and fell to `rank_candidates`'s original-slate-order tie-break instead of a real relevance-based rank.

A1's own submission script (`src/submit/codabench.py`) never hits this: its `--tier` defaults to `large`, so it always indexes and scores on the same corpus — there was never a train/test split of tiers for it to get out of sync in the first place. That is the real, structural asymmetry between the two submission paths, separate from (and additive to) the already-documented candidate-set-mismatch bug.

Ruled out as contributing causes (checked directly, not assumed): BM25 hyperparameters are identical between A1 and A2 (`k1=1.6, b=0.75, last_n=5`, confirmed against A1's own `mind_bm25_prediction.meta.json`); `rank_candidates`'s tie-breaking logic is the same function, copied verbatim from A1's `codabench.py`; `article_popularity` really is excluded from the submission-time model (no leak); the re-ranker never mutates or drops candidates from the real displayed slate.

**Fix**: after swapping `reranked.articles` to the test corpus, also call `reranked.index(list(test_articles.values()))` — `BM25Retriever.index()` fully rebuilds its internal state on every call, so re-indexing on the real 120,961-article test corpus is safe and cheap (a few seconds, confirmed directly: the second `Building index from IDs objects` block now appears in the log where it never did before). Verified on a 500-impression dev-limit smoke run before committing to the full run: predictions are non-trivial permutations (not the identity-order signature of the earlier `score_slate` key-mismatch bug), training still correctly indexes 65,238 small-tier articles, and the test-time index now correctly shows 120,961.

**Caveat, stated rather than hidden**: this fix is complete for plain BM25 (the only candidate generator with real Codabench evidence behind it, v4/v7). If `--candidate-generator` includes semantic search, `SemanticRetriever.index()` behaves differently when constructed from pre-computed vectors (the path this script uses) — calling `index()` again *filters* the already-encoded train vectors down to whatever ids overlap the new article set, rather than re-encoding the test corpus, so a semantic/fusion submission would still hit the same 11.4%-overlap problem even after this fix. Documented in-code as a known follow-up, not silently left for a future session to rediscover.

**Old v7 submission preserved, not deleted**, renamed to `mind_a2_rerank_bm25_k200_nopos_prediction.SUPERSEDED_0.5346_smalltier_bm25index.zip` so the pre-fix result and the bug it carried both stay traceable.

**v8, confirmed on the real Codabench leaderboard (submission id 934598, 2026-09-20 11:44): AUC 0.5570.** Same config as v7 (K=200, plain BM25, no display_position) with only the BM25-index-tier fix applied. **+0.0224 over v7 — by far the largest single gain in this pipeline's whole leaderboard history**, more than the combined effect of every earlier fix and tuning pass. This closes the entire remaining gap to A1's own plain-BM25 leaderboard baseline: **0.5570 vs A1's 0.5568, a difference of 0.0002 — within noise, effectively matched.**

This confirms the diagnosis directly: the BM25-index-tier bug (small-tier train corpus used to score against the large-tier test corpus, silently dropping ~46% of every slate's candidates to a tie-break fallback) was the dominant remaining cause of the whole v1→v7 shortfall, not some inherent ceiling on what a re-ranker built this way could achieve. **Q3's brief goal — "reproduce a baseline, then beat it" — is now honestly reachable rather than structurally blocked**: A2's re-ranker matches A1's own baseline for the first time, and any further real gain (tuning the re-ranker itself, recovering `article_popularity`'s signal at scale, or fixing the still-open semantic-index caveat noted above) would now show up as genuine re-ranker improvement rather than being masked by a candidate-scoring defect.

### Full MIND leaderboard progression, updated (2026-09-20)
| Version | Config | Score |
|---|---|---|
| v1–v6 | see table above | 0.5081–0.5320 |
| v7 | K=200, plain BM25, no display_position (BM25 index bug present) | 0.5346 |
| **v8** | same as v7, BM25 index fixed to cover the real test corpus | **0.5570 (best, matches A1's own baseline)** |

Not yet attempted: whether the re-ranker can genuinely *beat* A1's baseline now that the scoring defect is gone — this is the natural next experiment, but it was not run in this session (the brief's minimum bar — reproduce, then beat — is met at "reproduce" as of v8; "beat" remains open).

### Design note (2026-09-18)
- `report/design_note_full.md` — the comprehensive working design note: architecture, every Q1–Q5 design decision, the full bug-hunting narrative from this session, every measured results table, open items.
- `report/design_note.tex` / `.pdf` — the graded 6-page-target PDF, compressed from the above. 9pt twocolumn, 0.9cm margins (thin, per request), two TikZ flow diagrams (A1↔A2 architecture; Q2's retrieve-then-rank with the top-K/append ordering bug called out directly at the diamond node where it lived). Currently 2 of 6 pages — under target deliberately (brief: "avoid padding"), not for lack of content to report.

### Metrics gap review: recall@K, seed-robustness, and a serving-time feature-availability check (2026-09-19)

Went back through this note looking for measurements the pipeline had never taken, rather than assuming the v1–v6 leaderboard progression above was the whole story. Three gaps stood out, all now built INTO the pipeline (`a2src/rerank/candidates.py`, `scripts/run_a2.py`) rather than run as one-off scripts, so every future run reports them automatically:

**1. Recall@K was never measured.** Every training row the re-ranker ever saw HAS the click by construction — `retrieve_candidates`'s guaranteed append rescues any miss before the row reaches the training table (see the "third bug" section above). That makes the candidate generator's *actual* hit rate invisible from the training data itself: a generator that misses 95% of clicks organically and a generator that misses 5% look identical in the training table, because both get the click appended either way. Added `RetrievalCoverage` (`candidates.py`): counts, per impression, whether the click was found by the retriever's own top-K *before* any append — i.e. the genuine ceiling on what retrieval can hand the re-ranker. Wired into `run_a2.py` automatically (`--top-k` now defaults to 200, up from 150 — the brief's own stated range is "K ~ 100-200" and there was never a reason to sit at the bottom of it).

  Two smoke runs at MIND small-tier, 200/100 train/val impressions (202 with usable history) — deliberately tiny, just to prove the instrumentation works before a real-scale run:
  - BM25+semantic **union** candidate generator: recall@200 = **0.0149** (3/202)
  - BM25+semantic **RRF fusion** (the pipeline's default): recall@200 = **0.0149** (3/202) — byte-identical

  The two numbers matching exactly at this sample size is itself informative: it means the low recall is **shared small-sample noise, not a property of either candidate-generation strategy** — at n=202 there just aren't enough impressions with usable history for the two strategies to diverge yet. Not a conclusion to act on; recall@K needs a real-scale run (thousands of impressions) before it says anything about which candidate generator is actually better. Real-scale numbers are queued next.

**2. No seed-robustness check.** Every number in this note so far (all six MIND leaderboard versions, every offline AUC/nDCG) came from one training run each — no sense of how much any of it moves under nothing but LightGBM's own random seed. Added `--seeds N N N` to `run_a2.py`: retrains the same config once per seed via `train_reranker`'s existing `params={"seed": N}` override (no new plumbing needed — the option was already there, just never exercised), reports mean±sd AUC/nDCG@10.

  Smoke result (3 seeds, MIND small-tier, same tiny 200/100 slice): AUC and nDCG@10 were **identical across all three seeds** (`sd: 0.0`). At this sample size and with LightGBM's default params (no `bagging_fraction`/`feature_fraction`, which are the usual sources of seed-to-seed variance), that's expected rather than suspicious — the seed only controls stochastic subsampling, and none is enabled by `DEFAULT_PARAMS`. Whether variance appears at real scale, and whether it's worth enabling bagging to get an honest seed-sensitivity read, is an open question for the full run.

**3. No serving-time feature-availability check.** The single-feature ablation already in the pipeline (`run_ablation`, dropping `recency_weighted_click_count`/`history_click_count`) answers "how much does the re-ranker rely on recency," not "what happens if the click-log-derived features go stale or missing at serving time" — a real risk given `predict_submission.py`'s own train/serve-mismatch history (see the "real cause" section above: training-time assumptions that don't hold at serving time have already burned this pipeline once). Added `--feature-availability-check` to `run_a2.py`: reuses the *same* `run_ablation` machinery, just with all three click-log-derived features (`recency_weighted_click_count`, `history_click_count`, `session_click_count`) excluded together instead of one at a time, to get a single number for "what if the click log isn't available."

  Smoke result (same tiny slice, n=7 paired impressions): nDCG@10 dropped from 0.089 (full) to a mean of 0.127 *without* the click-log features — a positive, not negative, delta, but with a 95% CI spanning both signs (`[-0.129, 0.014]`, not significant) and n=7. Not interpretable at this sample size; flagged here only to confirm the instrumentation runs correctly end-to-end. Needs the real-scale run to mean anything.

**Also added, as an ablation (not the default): `UnionRetriever`** (`candidates.py`). Q2.1's candidate generator stays RRF fusion by default (`--candidate-generator bm25+semantic`); `bm25+semantic+union` is now a third selectable option that pools each component's own top-K/2 by plain set union instead of blending by reciprocal rank. The two combine candidates differently in a way that matters: RRF weights every document by *its rank in every component* (so a weak, mostly-agreeing component can still reweight the fused order), while union asks each component for a fixed slice outright and pools the ids with no cross-component reweighting. This is an additional comparison point, not a replacement — candidate-generation strategy itself (BM25 + semantic, fused or unioned) is unchanged from what Q2.1 already committed to; K moved from 150→200 within that same strategy, nothing more.

**Not changed:** candidate-generation composition (still BM25 + semantic, the same two retrievers as before) and FAISS is not newly introduced — `SemanticRetriever` already uses a brute-force/FAISS index internally at the appropriate scale (A1's own D4 decision, reused unchanged here). "Try union as an ablation" was the specific new request; it does not imply switching away from RRF as the default, and the smoke-test evidence above (recall@200 identical between the two at small scale) doesn't yet argue for switching either way.

**Next**: run recall@K, seed-repetition (3 seeds), the feature-availability check, and the union ablation at real scale on MIND (small-tier, uncapped) once memory allows, then repeat for EB-NeRD. `predict_submission.py` also got the same `--top-k` default (150→200) and `bm25+semantic+union` option, though it does not use `RetrievalCoverage` — it trains via `build_rerank_table_on_slate`, which never does a corpus-wide retrieval in the first place (every candidate is the real displayed slate), so there is no retrieval "miss" for recall@K to measure there.

### Full metrics-coverage audit: what this pipeline reports vs what the course names (2026-09-19)

Went through this pipeline's actual metric coverage systematically against two sources: the IRE lecture notes (`Notes/Module-1-Systems-Foundations.md`, `Notes/Module-2-Queryable-Index.md`) for what the course itself calls a standard measure, and this pipeline's own code, to check nothing course-relevant was silently skipped.

**Already covered, all reused directly from A1's harness (`src/eval/metrics.py`, `bootstrap.py`, `slices.py`) via `evaluate()` in Q5 of `run_a2.py`:**
- AUC, MRR, nDCG@K — the standard ranking-quality triad
- Recall@K (corpus regime) — now also `RetrievalCoverage`'s pre-append version, specific to this pipeline's append-on-miss construction
- Bootstrap CI (`bootstrap_ci`) and paired-difference CI (`paired_difference_ci`) — every reported delta in this note is a paired CI, not a bare number
- Cold/warm and head/tail slicing (`src/eval/slices.py`) — every Q5 run already reports these
- Diversity, novelty, coverage (beyond-accuracy metrics) — computed in the harness, reported in Q5's JSON
- Serving latency (p50/p90/p99/mean/max), cost per 1000 queries, cores-needed (`a2src/serving/bench.py`) — Q4

**Gap found and fixed: Precision@K.** The course's worked example (Module 1) pairs Precision@K with Recall@K as one of the four standard offline measures (alongside MRR, nDCG@K) — this pipeline computed the other three but never Precision@K. Added as a second field on `RetrievalCoverage` (`precision_at_k_sum`/`precision_at_k` property in `candidates.py`), reported alongside recall@K in both the log line and the JSON report.

**Honest caveat, stated directly rather than glossed over: Precision@K is nearly redundant with Recall@K on this dataset specifically.** A1's own code comment (`src/eval/metrics.py`, `recall_at_k`'s docstring) notes 99.5% of impressions have exactly one click. For a single-relevant-item impression, precision@K = (1 if the click is in the top-K else 0) / K = recall@K / K exactly — the two numbers differ only by the constant K, carrying no independent information here. Reported anyway because the course names it as a standard pair, and the redundancy itself is the honest finding for a single-relevant-item retrieval task (worth stating in the design note rather than padding it out as if it were a new signal).

**Deliberately out of scope, and why:**
- Online metrics (CTR, conversion, dwell time, abandonment) — this is an offline, historical-click-log assignment; there is no live traffic to measure them against. Not a gap, a scope boundary.
- Sketch/dedup structures (MinHash, LSH, Bloom filters, Count-Min) — these are Module 2's indexing/dedup concern, already handled once at the corpus level in A1 (MinHash dedup during index build), not a re-ranking-stage concern A2 needs to repeat.

No other course-named metric or statistic was found missing. This audit did not change candidate-generation strategy, K, or any modeling choice — it only added the one missing measurement (Precision@K) to the pipeline's existing reporting.

### Real-scale K=200 runs, and a fifth train/serve-mismatch-shaped finding in Q5's own harness path (2026-09-19)

First real-scale (60K train / 8K val impressions) run at K=200 with the new instrumentation, MIND, default fusion generator:

- **recall@200 = 0.0439, precision@200 = 0.000229** (2625/59735 impressions with usable history) — a real number now, not the earlier tiny-sample noise. For scale: only about 1 in 23 impressions has its click organically surface in the top-200 corpus-wide fusion retrieval before the guaranteed append rescues it.
- **Seed repetition (3 seeds) shows genuine, small variance at real scale**: AUC 0.9023 ± 0.0012, nDCG@10 0.3772 ± 0.0006 — unlike the tiny smoke test's `sd: 0.0`. LightGBM's default params have no bagging/feature-subsampling, so this small variance comes purely from tie-breaking and floating-point order effects in tree building, not from any resampling mechanism — worth keeping in mind as a lower bound on "how much noise exists even with nothing configured to be stochastic."
- **Feature importance at real scale**: `retrieval_score` 90.5%, `article_popularity` 9.1%, everything else under 0.2% combined — a much healthier, more interpretable picture than the tiny smoke test's popularity-dominated (92%) result, which was a small-sample artifact.
- **Feature-availability check**: dropping all three click-log-derived features costs essentially nothing (nDCG@10 mean diff +0.00009, 95% CI `[-0.0015, +0.0019]`, not significant, n=927 paired impressions). Consistent with the feature-importance finding above — MIND's re-ranker barely leans on the click-log features to begin with, so removing them doesn't move anything. **This means MIND's model would very likely survive a stale or unavailable click log at serving time without real degradation** — a genuinely reassuring, real finding, not a caveat to gloss over.
- **Before/after (on the training-distribution val table)**: base retriever AUC 0.023 → re-ranked AUC 0.905, a large, real improvement — the re-ranker is doing real work on the distribution it was trained on.

**But Q5's own harness-reported AUC came back at 0.502 [0.482, 0.521] — chance level — while MRR (0.257) and nDCG@10 (0.290) on the SAME impressions are clearly above chance.** Investigated why these two pictures (before/after's 0.905 and Q5's 0.502) disagree so sharply, since this is the exact shape of bug this pipeline has already been burned by twice (the missingness leak, the train/serve mismatch). Root cause, traced through `RerankedRetriever.retrieve()` (`a2src/rerank/reranked_retriever.py`) and A1's `harness.measure()` (`src/eval/harness.py`):

1. `RerankedRetriever.retrieve()` — the method `evaluate()` actually calls for Q5 — scores the re-ranker over the **corpus-wide top-200 candidates** (its own docstring already states, correctly, that this degrades `recency_weighted_click_count`/`session_click_count` to zero because the harness's `(history_text, k, at_time)` call shape carries no per-click timestamps or impression object).
2. `harness.measure()` then does `slate = imp.candidates; scores = dict(scored); ranked = rank_slate(scores, slate)` — i.e. it restricts those corpus-wide-200 scores onto the impression's OWN real slate (mean ~30 items on MIND), and anything in the real slate that the re-ranker's corpus-wide-200 candidate list never touched (which per the fresh recall@200 = 4.4% number is *most* of the real slate, most of the time) falls back to arbitrary slate order via `rank_slate`'s documented fallback.
3. AUC is sensitive to the FULL ranking (Mann-Whitney U over every pair), so when most of the slate is unscored and falls back to slate order, AUC collapses toward whatever slate order happens to be (near-chance here). MRR/nDCG@K only care about the TOP few ranks, so if even a few of the corpus-wide-200 candidates that DO land in the real slate happen to be correctly ranked highly, those metrics still pick up real signal — which is exactly the pattern observed (MRR/nDCG clearly above chance, AUC at chance).

**This is not a new bug in the sense of needing a code fix** — `RerankedRetriever.retrieve()`'s docstring already flags that it's a deliberately weaker path than `score_slate`, built only so Q5's harness has something to call. But the docstring's own framing ("does not depend on the degraded features") undersold the actual size of the effect: it's not just two zeroed features, it's the corpus-wide-vs-slate candidate-set mismatch (the SAME root cause as the "real cause" bug found earlier against the actual Codabench leaderboard) showing up a third time, this time inside Q5's own offline harness numbers specifically. **Q5's AUC number for the re-ranked retriever should be read as "how good is `retrieve()`'s degraded, corpus-wide-trained scoring when forced onto a real slate," not as "how good is the re-ranker."** The submission path (`predict_submission.py`, via `score_slate`) does not have this problem — it trains AND scores on the real slate throughout, which is exactly why `build_rerank_table_on_slate` was built. MRR/nDCG@K in Q5's report are more trustworthy than Q5's AUC for this reason; the `before_after` block in `run_a2.py`'s own report (0.905) is closer to the model's true quality on its own training distribution, and the real Codabench leaderboard score remains the only fully trustworthy number for serving-distribution performance (per the earlier "leaderboard as pipeline check" finding).

No code change made for this finding — it is a measurement-interpretation issue in how Q5's report should be read, not a training or feature bug. Documented here so the design note doesn't accidentally quote Q5's AUC as if it were the re-ranker's real leaderboard-equivalent quality.

### Fusion vs union at real scale on MIND: fusion wins on every measured axis (2026-09-19)

Same real-scale run (60K train / 8K val impressions), same everything except the candidate generator:

| | Fusion (RRF, default) | Union (ablation) |
|---|---|---|
| recall@200 | **0.0439** | 0.0416 |
| precision@200 | 0.000229 | 0.000216 |
| feature importance, top feature | `retrieval_score` 90.5% | `article_popularity` 82.9% |
| feature importance, 2nd | `article_popularity` 9.1% | `retrieval_score` 16.2% |
| before/after AUC (base → re-ranked) | 0.023 → **0.905** | 0.153 → 0.822 |

Unlike the earlier tiny (202-impression) smoke test, where the two candidate generators produced byte-identical recall@200 (0.0149 both, pure small-sample noise), this real-scale run (59,735 impressions with usable history) shows a small but consistent gap in fusion's favour on every axis measured. The more interesting difference isn't the recall gap itself (0.0439 vs 0.0416 is close) — it's the **feature-importance shape**: fusion's candidates are ranked well enough by `retrieval_score` alone that the re-ranker leans on it heavily (90.5%), while union's candidates apparently need `article_popularity` to compensate (82.9%) with `retrieval_score` contributing much less (16.2%). Plausible explanation, not yet directly tested: RRF's rank-based scoring gives `retrieval_score` a more informative, comparable scale across the fused candidate set (every candidate's score reflects agreement across both retrievers' ranks), whereas union's flat top-K/2-per-component split means a candidate's `retrieval_score` reflects only whichever single retriever surfaced it, a noisier and less comparable signal for the re-ranker to lean on — so the model falls back to popularity instead.

**Conclusion: this real-scale evidence supports keeping RRF fusion as the default candidate generator, as the pipeline already does.** The union ablation is a useful comparison point precisely because it shows the difference is real and measurable at scale (not because it suggests switching defaults) — exactly what "try union as an ablation, not a replacement" asked for.

### EB-NeRD vs MIND at real scale, fusion generator (2026-09-19/20)

Same real-scale run (60K train / 8K val impressions), same K=200 fusion generator, on EB-NeRD instead of MIND:

| | MIND | EB-NeRD |
|---|---|---|
| recall@200 | 0.0439 | **0.0263** (lower — EB-NeRD is the harder retrieval problem, consistent with A1's own findings) |
| precision@200 | 0.000229 | 0.000131 |
| seed AUC (mean ± sd, 3 seeds) | 0.9023 ± 0.0012 | 0.9282 ± 0.0170 |
| seed nDCG@10 (mean ± sd) | 0.3772 ± 0.0006 (near-flat) | **0.1480 ± 0.0690** (large swing: 0.069/0.237/0.137 across seeds 1/2/3 — roughly 3.4x between the worst and best seed) |
| feature importance, top 3 | retrieval_score 90.5%, article_popularity 9.1%, category_match 0.2% | retrieval_score 94.9%, article_popularity 2.7%, **freshness_hours 2.3%** |
| feature-availability check (drop click-log features) | nDCG10 diff +0.00009, CI crosses zero, **not significant** (n=927) | nDCG10 diff -0.0032, CI `[-0.0044,-0.0020]`, **significant** (n=6560) — but the model got *better*, not worse, without them |
| before/after AUC (base → re-ranked) | 0.023 → 0.905 | 0.016 → 0.897 (comparable-sized real improvement) |
| Q4 serving p50 / p99 latency | 0.39ms / 35.3ms | 9.3ms / 14.0ms (higher p50, since EB-NeRD's BM25 query scales with the user's whole click history — same finding this pipeline's Q4 discussion already made) |

**Three real, dataset-specific differences worth stating plainly:**

1. **EB-NeRD's re-ranker training is far less seed-stable than MIND's.** nDCG@10 swings 3.4x across three seeds on EB-NeRD (0.069 to 0.237) versus MIND's essentially flat 0.3772±0.0006. AUC is comparatively more stable on both (sd 0.017 vs 0.0012), so the instability is concentrated in the ranking-quality metric specifically, not the discrimination metric. Any single EB-NeRD offline number reported without multiple seeds risks being an unlucky (or lucky) draw rather than a representative result — this is the single most actionable finding from the seed-repetition check, and argues for reporting EB-NeRD numbers as a seed range/mean±sd rather than a point estimate wherever feasible.

2. **`freshness_hours` earns real, nonzero importance on EB-NeRD (2.3%) but is unavailable on MIND entirely** (MIND has no published_time; the feature is NaN there — see Q1's feature table). This matches the dataset-level distinction this pipeline has stated from the start: EB-NeRD carries genuine timestamps MIND doesn't, and the re-ranker is using that when it's there.

3. **The feature-availability check tells opposite-flavoured stories on the two datasets** — MIND: removing click-log features changes nothing (not significant); EB-NeRD: removing them is statistically significant but the model gets marginally *better* without them (nDCG10 +0.0032). Both point the same direction once combined with feature importance: on both datasets, `recency_weighted_click_count`/`history_click_count`/`session_click_count` individually contribute under 0.1% of gain, so on EB-NeRD their removal reads as mild denoising rather than a real loss of signal — **neither dataset's re-ranker actually depends on the click-log-derived features it was built to use**, which is itself worth stating honestly in the design note rather than assuming the original Q1 feature set earns its keep just because it was implemented.

As with the MIND section above, **Q5's own harness-computed AUC should not be read as the model's real quality** for the same corpus-wide-vs-slate reason already documented — prefer the `before_after` block or the eventual Codabench leaderboard score.

### Fusion vs union on EB-NeRD: same pattern as MIND, confirmed on a second dataset

| | Fusion | Union |
|---|---|---|
| recall@200 | 0.0263 | 0.0249 |
| feature importance, top | retrieval_score 94.9% | article_popularity 80.8% |
| before/after AUC | 0.016 → 0.897 | 0.212 → 0.837 |

Fusion beats union on all three axes here too, same as MIND — the structural pattern (union leans on popularity, fusion leans on retrieval_score; fusion produces a stronger re-ranker) replicates across both datasets, not just MIND. This strengthens the earlier conclusion: keep RRF fusion as the default candidate generator.

### Design note restructured to match the brief's Q6 exactly, bug narrative removed (2026-09-20)

Per explicit request: the graded design note (`report/design_note.tex`/`.pdf`) previously told the story chronologically — every leak found, every fix, the full v1–v8 bug-hunting narrative — which is exactly right for *this* tracking note but not what Q6 of the brief actually asks for. Q6's four bullets are: what was built and the key design choices, baseline-vs-improved with ablation and CI, serving/scale findings, and where the system breaks at 10×. Restructured the design note around those four headings plus an extended-evaluation section (Q5 requires it), removed:
- The entire "Four bugs found running the full pipeline" section and all bug-postmortem language throughout (every "bug found", "Bug 1/2/3/4", etc. reworded as a design decision or a comparison of attempts — e.g. "position-in-slate was tried and measurably hurt the leaderboard, so it's excluded" instead of "Bug 2: a label leak via position").
- Cost-per-1000-queries from both result tables (Q4 still asks for cost/QPS as an analysis point, but per instruction it's dropped from the report body — the code/results JSON still computes it if needed later).
- All narrative framing of the K=200 and BM25-index-tier changes as "bug fixes" — reworded as "three designs were tried, this one won" (a comparison table: shipped design vs. +display-position vs. +fusion candidates, each with its real leaderboard score), which is both more honest to what Q6 asks for (design choices + comparison) and simpler to read.

Kept, because Q6 explicitly asks for them: the offline baseline/ablation table with paired CI, the real-leaderboard comparison (still reports 0.5570 as the shipped design's score, just framed as "which design won" rather than "which bug got fixed"), the serving/scale numbers, the 10× breakage discussion, the extended-evaluation metrics, and the recall@K/seed-robustness/feature-availability findings from the metrics-gap review — these are genuine ablations and measurements, not bug narrative, so they stay per "variation in decision or attempts made is fine."

Recompiled: 6 pages (down from 8), matching the brief's target exactly rather than needing the "extend if justified" allowance — removing the bug narrative was enough on its own to hit target length. `design_note_full.md` (the larger working draft) is unchanged and keeps the full chronological account for reference; only the graded 6-page PDF was restructured.

### Design note expanded and simplified further; "Codabench submission" and "What's still open" sections removed (2026-09-20)

Following on from the restructure above: expanded the 6-page version with genuine explanatory content (not padding) throughout every section — plain-language explanations of what each behavioural feature actually answers, why a GBDT with a ranking objective was chosen over a neural ranker, what NRMS-lite's attention mechanism does in plain terms, why p99 (not an average) is the right latency number, what diversity/novelty/coverage each measure and why AUC/nDCG alone can't catch a bad-but-accurate recommender, why bootstrap confidence intervals matter, and a full walk-through of the 10× scaling argument (articles alone, traffic alone, and both together, each stressing a different part of the pipeline) — since the brief's own Q4.4 only asks for a scaling *argument*, not a new measurement, and no 10×-scale test exists in the code (`a2src/serving/bench.py` only has `measure_build_memory`, `measure_latency`, `cost_per_1000_queries`, confirmed by reading it directly rather than assuming).

Per explicit follow-up instructions: removed the "Codabench submission" section (its content was already covered inline elsewhere — the submission-format description folded into §3.3's discussion) and the "What's still open" section entirely, and added the team's names (Noel Alex Jacob, 2025201085; Emil Joji, 2025201040) to the title page in place of the placeholder "Team" author. Added a closing "Summary" section synthesizing the three decisions that carry most of the pipeline's weight and stating the honest headline result (parity with A1's baseline, not yet beating it) directly, since the note previously ended abruptly after the last ablation table with no closing synthesis.

Recompiles cleanly to 8 pages with no undefined references. This is longer than the 6-page target but stays within the brief's own "extend if your content justifies the extra length, but avoid padding" allowance — every addition is a real explanation or a real measurement's context, not repetition or filler.
