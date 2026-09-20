"""Q3.1: baseline reproduction -- a reduced NRMS (Wu et al. 2019).

Full NRMS has a token-level self-attention news encoder that turns each
article's title into a vector, THEN an additive-attention user encoder that
pools a user's history of those vectors, THEN a dot-product scorer. This
"lite" version keeps the part the brief actually asks to be reproduced --
NRMS's defining idea, additive attention over history -- and replaces the
news encoder with A1's already-cached MiniLM sentence embeddings
(``src.retrieval.encode.encode_cached``) rather than training a token-level
encoder from scratch, which the brief explicitly allows ("NRMS-style or a
simple MLP", Q2.2 Option B) and which is the difference between minutes and
GPU-hours of training at this data scale.

Reused as-is from the teammate's Kaggle notebook design (Section 29,
``pipeline/nrms_lite.py``): the additive-attention module and the
negative-sampled softmax training loop there are correct NRMS reproductions
and are not reinvented here, only re-hosted against A1's data types and
retrained/verified locally instead of taking them on faith.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
import torch.nn as nn

_A1_ROOT = Path(__file__).resolve().parents[2] / "Assignment-1-Lexical-Semantic-Retrieval"
if str(_A1_ROOT) not in sys.path:
    sys.path.insert(0, str(_A1_ROOT))

from src.data.schema import History, Impression  # noqa: E402


class AdditiveAttention(nn.Module):
    """NRMS's user encoder: attention-weighted pool over history embeddings."""

    def __init__(self, dim: int, hidden: int = 64) -> None:
        super().__init__()
        self.proj = nn.Linear(dim, hidden)
        self.query = nn.Linear(hidden, 1, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if x.shape[0] == 0:
            return torch.zeros(x.shape[-1])
        scores = self.query(torch.tanh(self.proj(x))).squeeze(-1)
        weights = torch.softmax(scores, dim=0)
        return (weights.unsqueeze(-1) * x).sum(dim=0)


class NRMSLite(nn.Module):
    def __init__(self, emb_dim: int) -> None:
        super().__init__()
        self.attn = AdditiveAttention(emb_dim)

    def forward(self, history_embs: torch.Tensor, candidate_embs: torch.Tensor) -> torch.Tensor:
        user_vec = self.attn(history_embs)
        return candidate_embs @ user_vec


def build_training_examples(
    impressions: Iterable[Impression],
    histories: dict[str, History],
    article_vecs: dict[str, np.ndarray],
    max_history: int = 50,
    max_examples: int | None = None,
) -> list[tuple[torch.Tensor, torch.Tensor, int]]:
    """One ``(history_embs, candidate_embs, positive_idx)`` tuple per labelled
    impression with at least one clicked candidate whose embedding exists.

    History is truncated to the leakage boundary via ``hist.before(imp.time)``
    -- the same primitive Q1's features use -- so the baseline is trained
    under the identical no-future-leakage guarantee, not a separate,
    unverified path.
    """
    out = []
    for imp in impressions:
        if not imp.is_labelled:
            continue
        hist = histories.get(imp.user_id)
        if hist is None:
            continue
        past = hist.before(imp.time)[-max_history:]
        hist_vecs = [article_vecs[a] for a in past if a in article_vecs]
        cand_vecs, pos_idx = [], None
        clicked = set(imp.clicked)
        for i, aid in enumerate(imp.candidates):
            if aid not in article_vecs:
                continue
            if pos_idx is None and aid in clicked:
                pos_idx = len(cand_vecs)
            cand_vecs.append(article_vecs[aid])
        if pos_idx is None or len(cand_vecs) < 2 or not hist_vecs:
            continue  # nothing to learn from: no positive, or a single-candidate slate
        out.append((
            torch.tensor(np.stack(hist_vecs), dtype=torch.float32),
            torch.tensor(np.stack(cand_vecs), dtype=torch.float32),
            pos_idx,
        ))
        if max_examples and len(out) >= max_examples:
            break
    return out


def train_nrms_lite(
    model: NRMSLite,
    examples: list[tuple[torch.Tensor, torch.Tensor, int]],
    epochs: int = 3,
    lr: float = 1e-3,
    accumulate: int = 16,
) -> list[float]:
    """Standard NRMS training: negative-sampled softmax cross-entropy, the
    positive against the rest of that impression's own candidate slate.
    Gradient-accumulated over ``accumulate`` impressions to approximate
    mini-batching without padding variable-length histories/slates."""
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss()
    model.train()
    epoch_losses = []
    for _ in range(epochs):
        total, n_seen = 0.0, 0
        opt.zero_grad()
        for hist_e, cand_e, pos_idx in examples:
            scores = model(hist_e, cand_e).unsqueeze(0)
            loss = loss_fn(scores, torch.tensor([pos_idx])) / accumulate
            loss.backward()
            total += loss.item() * accumulate
            n_seen += 1
            if n_seen % accumulate == 0:
                opt.step()
                opt.zero_grad()
        opt.step()
        epoch_losses.append(total / max(n_seen, 1))
    return epoch_losses


@torch.no_grad()
def score_impression(
    model: NRMSLite,
    imp: Impression,
    past_click_ids: list[str],
    article_vecs: dict[str, np.ndarray],
    max_history: int = 50,
) -> dict[str, float]:
    """Score every candidate of one impression. Returns {} if there is no
    usable history or no candidate has an embedding -- a cold-start impression
    this architecture cannot answer, consistent with how A1's harness treats
    an empty-history retrieval (measure(): empty texts -> [])."""
    model.eval()
    past = past_click_ids[-max_history:]
    hist_vecs = [article_vecs[a] for a in past if a in article_vecs]
    if not hist_vecs:
        return {}
    ids, vecs = [], []
    for aid in imp.candidates:
        if aid in article_vecs:
            ids.append(aid)
            vecs.append(article_vecs[aid])
    if not ids:
        return {}
    hist_t = torch.tensor(np.stack(hist_vecs), dtype=torch.float32)
    cand_t = torch.tensor(np.stack(vecs), dtype=torch.float32)
    scores = model(hist_t, cand_t).numpy()
    return dict(zip(ids, scores.tolist()))
