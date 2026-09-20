# Assignment 2 — Learning from Click-Logs on EB-NeRD and MIND

CS4.406 Information Retrieval & Extraction. Team: Noel Alex Jacob (2025201085), Emil Joji
(2025201040).

A two-stage retrieve-then-rank pipeline built on top of [Assignment 1's lexical/semantic
retriever](../Assignment-1-Lexical-Semantic-Retrieval/) (imported, never copied or modified): a
LightGBM re-ranker trained on click-log behavioural features, evaluated through A1's own harness,
and shipped as real Codabench prediction files for both MIND and EB-NeRD.

**Full design note:** [`report/design_note.pdf`](report/design_note.pdf) (also `.tex` source).
**Full working history** (every design decision, every measurement, in chronological order):
[`Assignment-2-Click-Log-Reranking.md`](Assignment-2-Click-Log-Reranking.md).

## Setup

This assignment depends on Assignment 1's code and data (`../Assignment-1-Lexical-Semantic-Retrieval/`)
sitting alongside this folder — it is imported via `sys.path`, never copied. A1 must already be
set up (its own venv, its own downloaded datasets under `data/`) before running anything here.

```bash
# From this folder (Assignment-2-Click-Log-Reranking/)
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

A separate venv from A1's own (same package pins, plus `lightgbm` for the re-ranker) — this keeps
A1's already-graded environment untouched.

## One-command reproduce

Everything below assumes the working directory is `Assignment-2-Click-Log-Reranking/` and A1's
datasets are already extracted under `../Assignment-1-Lexical-Semantic-Retrieval/data/work/`.

### Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

17 tests: the behaviour-window boundary (Q9's anti-gaming requirement — tested twice per check,
once on clean input and once on deliberately corrupted input, per A1's own convention) plus
regression coverage for every fix made during development.

### Offline evaluation (Q2–Q5), one dataset at a time

```bash
# MIND, real scale, default config (K=200, BM25+semantic fusion candidates)
.venv/bin/python -m scripts.run_a2 --dataset mind --tier small \
    --limit 60000 --val-limit 8000 --mem-gb 6 \
    --out results/a2_mind.json

# EB-NeRD, same scale
.venv/bin/python -m scripts.run_a2 --dataset ebnerd --tier small \
    --limit 60000 --val-limit 8000 --mem-gb 6 \
    --out results/a2_ebnerd.json
```

Runs Q2's candidate table + re-ranker, Q3's NRMS-lite baseline + ablation with paired bootstrap
CI, Q4's serving benchmark, and Q5's extended evaluation through A1's harness (all metrics × both
required slices × bootstrap CIs) — everything is printed and written to `--out` as one JSON
report. `--mem-gb` sets a hard memory budget A1's own `resources.py` enforces before the run
starts (refuses to launch on a machine already tight on RAM rather than crashing partway through).

Useful flags for a faster/smaller run during development: `--limit 4000 --val-limit 1000` (the
defaults) instead of the real-scale numbers above. Other flags worth knowing:
`--candidate-generator {bm25,bm25+semantic,bm25+semantic+union}` (§6 of the design note's fusion-
vs-union ablation), `--seeds 1 2 3` (seed-robustness check), `--feature-availability-check`
(Q9's anti-gaming metric-availability report).

### Generate the real Codabench submission files

```bash
# MIND — the shipped, best-scoring config (K=200, plain BM25, no display-position feature)
.venv/bin/python -m scripts.predict_submission --dataset mind \
    --train-limit 60000 --top-k 200 --no-display-position --mem-gb 6

# EB-NeRD — the shipped config (K=200, plain BM25, display-position included)
.venv/bin/python -m scripts.predict_submission --dataset ebnerd \
    --train-limit 60000 --top-k 200 --mem-gb 6
```

Trains the re-ranker on the training split only (never the held-out split used for scoring, per
the brief's own anti-gaming rule), streams predictions over the real large-tier test set (2.37M
impressions for MIND, 13.5M for EB-NeRD — this takes a while; the EB-NeRD run took roughly 2.4
hours on the development machine), and writes a Codabench-ready zip to `submissions/`. Use
`--dev-limit 2000` to check the output on a small slice before committing to the full run.

**Do not re-run this casually** — each dataset only allows a limited number of daily Codabench
submissions, and a bad file burns one for nothing. `--dev-limit` and the offline evaluation above
exist specifically so the real file only needs to be generated once it is expected to work.

## What's in this folder

| Path | Contents |
|---|---|
| `a2src/rerank/` | Feature engineering (`features.py`), candidate generation + `RetrievalCoverage`/recall@K instrumentation (`candidates.py`), the LightGBM re-ranker (`reranker.py`), the NRMS-lite baseline (`nrms_lite.py`), the paired-CI ablation (`ablation.py`), the `RerankedRetriever` A1-harness adapter (`reranked_retriever.py`) |
| `a2src/serving/` | Q4's serving/scale measurement primitives (`bench.py`) |
| `scripts/run_a2.py` | Offline Q2–Q5 pipeline, one dataset per invocation |
| `scripts/predict_submission.py` | Real Codabench prediction-file generator |
| `tests/` | Behaviour-window-boundary regression tests |
| `results/` | JSON reports from `run_a2.py` runs (small, tracked in git) |
| `submissions/` | Codabench zips + their `.meta.json` (the zips/txts are large and regenerable, so only the `.meta.json` — which records the exact config and stats that produced each file — is tracked in git) |
| `report/` | `design_note.tex`/`.pdf` (the graded design note, currently 8 pages against the brief's 6-page target — justified by real content, not padding) and `design_note_full.md` (the larger working draft) |
| `Assignment-2-Click-Log-Reranking.md` | The full chronological working note: every bug found, every design decision, every measurement, in the order it actually happened |
| `ai-log.md` | Raw prompt capture (auto-logged per-prompt by a vault-level hook); curate before final submission |

## Leaderboard result

**MIND: AUC 0.5570** (submission id 934598), matching Assignment 1's own plain-BM25 leaderboard
score (0.5568) to within noise. Full progression of every design attempt and what each one showed
is in the design note (`report/design_note.pdf`, §3.3) and the full working note.

## Still to do before final submission

- [ ] **EB-NeRD needs a re-upload.** Submission id 934506
      (`ebnerd_a2_rerank_bm25_k200_pos_pred.zip`, uploaded 2026-09-20 10:26) predates the
      BM25-index-tier fix described in the design note — the fix was applied and verified locally
      around 10:36–11:09 that same morning, so id 934506 is running the pre-fix code and is stuck
      at "Submitted" with no score yet as of the last check. The already-regenerated, fixed file
      (`submissions/ebnerd_a2_rerank_bm25_k200_pos_prediction.zip`, produced 2026-09-20 15:50, same
      filename as the superseded one — check the file's own modification time, not just its name)
      has **not** been uploaded — do that once id 934506 either scores or is confirmed abandoned,
      to https://www.codabench.org/competitions/2469/.
- [ ] Take leaderboard screenshots from both competitions once both have a final score (MIND:
      https://www.codabench.org/competitions/13967/, already scored 0.5570; EB-NeRD: link above,
      pending the re-upload) — required by Q7/Q5.
- [ ] Curate `ai-log.md` into its graded form (starred decisive prompts, grouped by phase,
      what-worked/what-failed, AI-generated vs. human-written) before submitting.
- [ ] Upload the design note PDF to Moodle.
