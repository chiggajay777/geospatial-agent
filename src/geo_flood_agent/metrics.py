"""Threshold-free scores for a continuous susceptibility vs a binary label.

Implemented here so validation does not depend on scikit-learn.
"""

from __future__ import annotations

import numpy as np


def auroc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """AUROC via average ranks (Mann–Whitney), with tie averaging.

    Returns None when either class is missing.
    """
    y = np.asarray(y_true).astype(np.uint8).ravel()
    s = np.asarray(scores, dtype=np.float64).ravel()
    if y.shape != s.shape:
        raise ValueError("y_true and scores must have the same shape")
    n_pos = int(y.sum())
    n_neg = int(y.size - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(s, kind="mergesort")
    sorted_s = s[order]
    ranks = np.empty(y.size, dtype=np.float64)
    i = 0
    n = y.size
    while i < n:
        j = i + 1
        while j < n and sorted_s[j] == sorted_s[i]:
            j += 1
        avg = 0.5 * ((i + 1) + j)
        ranks[order[i:j]] = avg
        i = j
    sum_pos = float(ranks[y == 1].sum())
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def average_precision(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """Non-interpolated average precision (area under the precision-recall curve)."""
    y = np.asarray(y_true).astype(np.float64).ravel()
    s = np.asarray(scores, dtype=np.float64).ravel()
    n_pos = float(y.sum())
    if n_pos <= 0 or n_pos >= y.size:
        return None
    order = np.argsort(-s, kind="mergesort")
    y_sorted = y[order]
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(1.0 - y_sorted)
    precision = tp / (tp + fp)
    return float(precision[y_sorted == 1].sum() / n_pos)


def precision_recall_at(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, float | None]:
    y = np.asarray(y_true).astype(bool).ravel()
    s = np.asarray(scores, dtype=np.float64).ravel()
    pred = s >= threshold
    tp = int(np.count_nonzero(pred & y))
    fp = int(np.count_nonzero(pred & ~y))
    fn = int(np.count_nonzero(~pred & y))
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    return {
        "threshold": float(threshold),
        "precision": None if prec is None else float(prec),
        "recall": None if rec is None else float(rec),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def roc_pr_curves(y_true: np.ndarray, scores: np.ndarray, n_points: int = 200) -> dict[str, list[float]]:
    """Decimated ROC and PR curves for plotting. Metrics themselves use auroc/AP."""
    y = np.asarray(y_true).astype(np.float64).ravel()
    s = np.asarray(scores, dtype=np.float64).ravel()
    n_pos = float(y.sum())
    n_neg = float(y.size - n_pos)
    order = np.argsort(-s, kind="mergesort")
    y_sorted = y[order]
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(1.0 - y_sorted)
    if y.size > n_points:
        idx = np.linspace(0, y.size - 1, n_points).astype(np.int64)
    else:
        idx = np.arange(y.size)
    tpr = (tp[idx] / n_pos).tolist() if n_pos else []
    fpr = (fp[idx] / n_neg).tolist() if n_neg else []
    prec = (tp[idx] / np.maximum(tp[idx] + fp[idx], 1e-12)).tolist()
    rec = (tp[idx] / n_pos).tolist() if n_pos else []
    return {"fpr": fpr, "tpr": tpr, "precision": prec, "recall": rec}
