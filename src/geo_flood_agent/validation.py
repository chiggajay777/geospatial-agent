"""Susceptibility vs Sen1Floods11 hand-labeled flood water.

The official hand-labeled test split is used
(`gs://sen1floods11/v1.1/splits/flood_handlabeled/flood_test_data.csv`, public HTTPS).

For each chip:
- observed water is LabelHand == 1; non-water is 0; -1 is nodata
- permanent water is JRCWaterHand > 0 and is excluded
- the score is the same terrain concern as the live product (HAND + slope only)

JRC occurrence is not a predictor. Occurrence history can contain the flood
events themselves, so using it to "predict" the labels would leak.

Parameters match geo_flood_agent.constants and are not fit on this split.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from geo_flood_agent.aoi import aoi_from_bbox
from geo_flood_agent.cog import TransferStats
from geo_flood_agent.constants import SCREEN_THRESHOLD
from geo_flood_agent.dem import fetch_dem_grid
from geo_flood_agent.metrics import auroc, average_precision, precision_recall_at, roc_pr_curves

S1_BASE = "https://storage.googleapis.com/sen1floods11/v1.1"
LABEL_URL = S1_BASE + "/data/flood_events/HandLabeled/LabelHand/{name}"
JRC_URL = S1_BASE + "/data/flood_events/HandLabeled/JRCWaterHand/{name}"
TEST_CSV_URL = S1_BASE + "/splits/flood_handlabeled/flood_test_data.csv"
USER_AGENT = "geo-flood-agent/0.3 (Sen1Floods11 validation)"

SCORE_DEFINITION = (
    "Terrain concern on non-permanent pixels: "
    "100 * (0.8 * clip((10 - HAND_m) / 10, 0, 1) + 0.2 * clip((5 - slope_deg) / 5, 0, 1)). "
    "HAND from Copernicus GLO-30 (D8, 0.5 km² streams, 3 km buffer, 30 m, coarsened only if the grid would exceed 1200 px). "
    "Permanent water = Sen1Floods11 JRCWaterHand > 0. "
    "JRC occurrence is not a predictor. "
    "Operating point score >= 50 was fixed in constants.py and was not tuned on this split."
)


def _download(url: str, dest: Path, byte_counter: dict[str, int]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        byte_counter["cache_hit_bytes"] = byte_counter.get("cache_hit_bytes", 0) + dest.stat().st_size
        return
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    dest.write_bytes(data)
    byte_counter["downloaded_bytes"] = byte_counter.get("downloaded_bytes", 0) + len(data)


def load_test_chip_names() -> list[str]:
    req = urllib.request.Request(TEST_CSV_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("s1"):
            # The published file has no header; keep a guard anyway.
            if "label" in line.lower() and names == []:
                continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) < 2:
            continue
        label = parts[1]
        if not label.endswith(".tif"):
            continue
        names.append(label)
    if not names:
        raise RuntimeError("Sen1Floods11 test split CSV contained no label names")
    return names


def select_chips(names: list[str], max_per_event: int | None) -> list[str]:
    """CSV order, optionally the first N chips of each event. Not chosen by score."""
    if not max_per_event or max_per_event <= 0:
        return list(names)
    counts: dict[str, int] = {}
    chosen: list[str] = []
    for name in names:
        event = name.split("_", 1)[0]
        if counts.get(event, 0) >= max_per_event:
            continue
        counts[event] = counts.get(event, 0) + 1
        chosen.append(name)
    return chosen


def _sample_score(concern: np.ndarray, grid: dict, label_ds) -> np.ndarray:
    import rasterio
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, reproject

    src = np.where(np.isfinite(concern), concern, -9999.0).astype(np.float32)
    dest = np.full((label_ds.height, label_ds.width), -9999.0, dtype=np.float32)
    with rasterio.Env(GDAL_NUM_THREADS="1"):
        reproject(
            source=src,
            destination=dest,
            src_transform=Affine(*grid["transform"]),
            src_crs=grid["crs"],
            src_nodata=-9999.0,
            dst_transform=label_ds.transform,
            dst_crs=label_ds.crs,
            dst_nodata=-9999.0,
            resampling=Resampling.bilinear,
        )
    out = dest.astype(np.float64)
    out[out <= -9998] = np.nan
    return out


def _chip_arrays(label_path: Path, jrc_path: Path) -> tuple[np.ndarray, np.ndarray, Any]:
    import rasterio

    with rasterio.open(label_path) as lab:
        label = lab.read(1)
        profile = {
            "height": lab.height,
            "width": lab.width,
            "transform": lab.transform,
            "crs": lab.crs,
            "bounds": lab.bounds,
        }
    with rasterio.open(jrc_path) as jrc_ds:
        jrc = jrc_ds.read(1)
    return label, jrc, profile


class _LabelView:
    """Tiny adapter so _sample_score can use rasterio's reproject destination metadata."""

    def __init__(self, profile: dict):
        self.height = profile["height"]
        self.width = profile["width"]
        self.transform = profile["transform"]
        self.crs = profile["crs"]


def evaluate_chip(
    label_name: str,
    cache: Path,
    transfer: TransferStats,
    chip_bytes: dict[str, int],
) -> dict[str, Any]:
    event = label_name.split("_", 1)[0]
    label_path = cache / "labels" / label_name
    jrc_name = label_name.replace("_LabelHand.tif", "_JRCWaterHand.tif")
    jrc_path = cache / "jrc" / jrc_name
    _download(LABEL_URL.format(name=label_name), label_path, chip_bytes)
    _download(JRC_URL.format(name=jrc_name), jrc_path, chip_bytes)
    label, jrc, profile = _chip_arrays(label_path, jrc_path)
    if label.shape != jrc.shape:
        return {"chip": label_name, "event": event, "status": "skip", "reason": "label/jrc shape mismatch"}
    bounds = profile["bounds"]
    aoi = aoi_from_bbox(bounds.left, bounds.bottom, bounds.right, bounds.top)
    dem = fetch_dem_grid(aoi, stats=transfer)
    score = _sample_score(dem["concern"], dem["grid"], _LabelView(profile))
    permanent = jrc > 0
    valid = (label >= 0) & np.isfinite(score) & ~permanent
    y = (label == 1) & valid
    n_pos = int(y.sum())
    n_neg = int((valid & (label == 0)).sum())
    n_perm = int(permanent.sum())
    n_nodata = int((label < 0).sum())
    jrc_values, jrc_counts = np.unique(jrc, return_counts=True)
    row = {
        "chip": label_name,
        "event": event,
        "status": "ok",
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_permanent": n_perm,
        "n_nodata_label": n_nodata,
        "n_scored": int(valid.sum()),
        "jrc_values": {int(v): int(c) for v, c in zip(jrc_values.tolist(), jrc_counts.tolist())},
        "res_m": dem["grid"]["res_m"],
        "dem_sources": dem["sources"],
    }
    if n_pos == 0 or n_neg == 0:
        row["status"] = "skip"
        row["reason"] = "need both flooded and non-flooded land pixels after the permanent-water mask"
        return row
    y_valid = (label[valid] == 1).astype(np.uint8)
    s_valid = score[valid].astype(np.float32)
    if n_pos >= 20 and n_neg >= 20:
        row["auroc"] = auroc(y_valid, s_valid)
        row["average_precision"] = average_precision(y_valid, s_valid)
    else:
        row["auroc"] = None
        row["average_precision"] = None
        row["auroc_note"] = "chip metrics omitted (fewer than 20 pixels in a class); pixels still enter the pool"
    row["_y"] = y_valid
    row["_s"] = s_valid
    return row


def _pool_metrics(y: np.ndarray, s: np.ndarray) -> dict[str, Any]:
    pr = precision_recall_at(y, s, SCREEN_THRESHOLD)
    return {
        "n_pixels": int(y.size),
        "n_pos": int(y.sum()),
        "n_neg": int(y.size - int(y.sum())),
        "auroc": auroc(y, s),
        "average_precision": average_precision(y, s),
        "at_threshold": pr,
        "prevalence": float(y.mean()) if y.size else None,
    }


def _figures(summary: dict, out_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    curves = summary.get("curves") or {}
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.2))
    ax = axes[0]
    ax.plot(curves.get("fpr") or [0, 1], curves.get("tpr") or [0, 1], color="#1d3557", lw=2)
    ax.plot([0, 1], [0, 1], color="#888", ls="--", lw=1)
    ax.set_xlabel("False positive rate")
    ax.set_ylabel("True positive rate")
    auc = summary["pooled"]["auroc"]
    ax.set_title(f"Pooled ROC  AUROC={auc:.3f}" if auc is not None else "Pooled ROC")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax = axes[1]
    ax.plot(curves.get("recall") or [0, 1], curves.get("precision") or [1, 0], color="#e76f51", lw=2)
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ap = summary["pooled"]["average_precision"]
    ax.set_title(f"Pooled PR  AP={ap:.3f}" if ap is not None else "Pooled PR")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.tight_layout()
    roc_path = fig_dir / "validation_roc_pr.png"
    fig.savefig(roc_path, dpi=130)
    plt.close(fig)

    events = summary.get("by_event") or []
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    if events:
        labels = [e["event"] for e in events]
        vals = [e["auroc"] if e["auroc"] is not None else 0 for e in events]
        colors = ["#2a9d8f" if e["auroc"] is not None else "#cccccc" for e in events]
        ax.barh(labels, vals, color=colors)
        ax.axvline(0.5, color="#888", ls="--", lw=1, label="0.5")
        ax.set_xlim(0, 1.18)
        for i, e in enumerate(events):
            txt = "n/a" if e["auroc"] is None else f"{e['auroc']:.2f}"
            x = 0.02 if e["auroc"] is None else min(float(e["auroc"]) + 0.02, 1.02)
            ax.text(x, i, f"{txt}   flood px {e['n_pos']}", va="center", fontsize=8, color="#1d3557")
    ax.set_xlabel("AUROC")
    ax.set_title("Sen1Floods11 test split — terrain susceptibility by event")
    fig.tight_layout()
    bar_path = fig_dir / "validation_by_event.png"
    fig.savefig(bar_path, dpi=130)
    plt.close(fig)
    return [str(roc_path), str(bar_path)]


def run_validation(
    out_dir: Path,
    cache_dir: Path,
    *,
    max_per_event: int | None = None,
) -> dict[str, Any]:
    """Download the requested test chips, score them, and write a summary + figures."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir)
    names = load_test_chip_names()
    chosen = select_chips(names, max_per_event)
    transfer = TransferStats()
    chip_bytes = {"downloaded_bytes": 0, "cache_hit_bytes": 0}
    rows: list[dict] = []
    pooled_y: list[np.ndarray] = []
    pooled_s: list[np.ndarray] = []
    by_event_y: dict[str, list[np.ndarray]] = {}
    by_event_s: dict[str, list[np.ndarray]] = {}
    print(f"validation chips: {len(chosen)} of {len(names)} in the official test split")
    for i, name in enumerate(chosen, start=1):
        print(f"  [{i}/{len(chosen)}] {name}", flush=True)
        try:
            row = evaluate_chip(name, cache_dir, transfer, chip_bytes)
        except Exception as exc:  # noqa: BLE001 — one chip should not abort the table
            row = {
                "chip": name,
                "event": name.split("_", 1)[0],
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        y = row.pop("_y", None)
        s = row.pop("_s", None)
        rows.append(row)
        if y is not None:
            pooled_y.append(y)
            pooled_s.append(s)
            by_event_y.setdefault(row["event"], []).append(y)
            by_event_s.setdefault(row["event"], []).append(s)
        print(
            f"      status={row.get('status')} n_pos={row.get('n_pos')} n_neg={row.get('n_neg')} "
            f"dem_get_bytes={transfer.http_get_bytes}",
            flush=True,
        )

    if pooled_y:
        y_all = np.concatenate(pooled_y)
        s_all = np.concatenate(pooled_s)
        pooled = _pool_metrics(y_all, s_all)
        curves = roc_pr_curves(y_all, s_all)
    else:
        pooled = {"n_pixels": 0, "n_pos": 0, "n_neg": 0, "auroc": None, "average_precision": None}
        curves = {}

    by_event = []
    for event in sorted(by_event_y):
        y = np.concatenate(by_event_y[event])
        s = np.concatenate(by_event_s[event])
        metrics = _pool_metrics(y, s)
        # Event AUROC is only reported when both classes are reasonably populated.
        if metrics["n_pos"] < 50 or metrics["n_neg"] < 50:
            metrics["auroc_reported"] = None
            metrics["note"] = "fewer than 50 pixels in one class; AUROC withheld"
        else:
            metrics["auroc_reported"] = metrics["auroc"]
        by_event.append(
            {
                "event": event,
                "n_chips": sum(1 for r in rows if r.get("event") == event and r.get("status") == "ok"),
                "n_pos": metrics["n_pos"],
                "n_neg": metrics["n_neg"],
                "auroc": metrics.get("auroc_reported", metrics["auroc"]),
                "average_precision": metrics["average_precision"] if metrics["n_pos"] >= 50 else None,
                "prevalence": metrics["prevalence"],
            }
        )
    macro = [e["auroc"] for e in by_event if e["auroc"] is not None]
    summary: dict[str, Any] = {
        "dataset": "Sen1Floods11 v1.1 hand-labeled flood test split",
        "citation": "Bonafilia et al. 2020, CVPR Workshops",
        "split_url": TEST_CSV_URL,
        "score_definition": SCORE_DEFINITION,
        "n_test_chips_in_split": len(names),
        "n_chips_attempted": len(chosen),
        "max_per_event": max_per_event or None,
        "selection": (
            "all chips in flood_test_data.csv, file order"
            if not max_per_event
            else f"first {max_per_event} chips per event in flood_test_data.csv order (not chosen by score)"
        ),
        "n_chips_scored": sum(1 for r in rows if r.get("status") == "ok"),
        "n_chips_skipped": sum(1 for r in rows if r.get("status") != "ok"),
        "pooled": pooled,
        "macro_auroc_unweighted_events": (float(np.mean(macro)) if macro else None),
        "by_event": by_event,
        "bytes": {
            "sen1floods11_downloaded_bytes": chip_bytes["downloaded_bytes"],
            "sen1floods11_cache_hit_bytes": chip_bytes["cache_hit_bytes"],
            "copernicus_http_get_bytes": transfer.http_get_bytes,
            "copernicus_largest_head_content_length": transfer.http_head_bytes_reported,
        },
        "curves": curves,
        "chips": rows,
    }
    fig_paths = _figures(summary, out_dir) if pooled.get("n_pixels") else []
    # Curves are large enough; keep them in the JSON (decimated) so the figure is reproducible.
    summary["figures"] = [Path(p).name for p in fig_paths]
    out_json = out_dir / "validation_summary.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = out_dir / "validation_chips.csv"
    fieldnames = [
        "chip",
        "event",
        "status",
        "reason",
        "n_pos",
        "n_neg",
        "n_permanent",
        "n_scored",
        "auroc",
        "average_precision",
        "res_m",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    md = _markdown(summary)
    (out_dir / "VALIDATION.md").write_text(md, encoding="utf-8")
    print(md)
    return summary


def _markdown(summary: dict) -> str:
    p = summary["pooled"]
    at = p.get("at_threshold") or {}

    def r(v, d=3):
        if v is None:
            return "n/a"
        return f"{float(v):.{d}f}"

    lines = [
        "# Sen1Floods11 validation",
        "",
        summary["score_definition"],
        "",
        f"Selection: {summary['selection']}.",
        f"Attempted {summary['n_chips_attempted']} of {summary['n_test_chips_in_split']} test chips; "
        f"scored {summary['n_chips_scored']}; skipped/errored {summary['n_chips_skipped']}.",
        "",
        "## Pooled (pixel-weighted)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Pixels scored | {p.get('n_pixels')} |",
        f"| Flooded land pixels | {p.get('n_pos')} |",
        f"| Non-flooded land pixels | {p.get('n_neg')} |",
        f"| Prevalence | {r(p.get('prevalence'))} |",
        f"| AUROC | {r(p.get('auroc'))} |",
        f"| Average precision | {r(p.get('average_precision'))} |",
        f"| Precision at score≥{at.get('threshold', 50)} | {r(at.get('precision'))} |",
        f"| Recall at score≥{at.get('threshold', 50)} | {r(at.get('recall'))} |",
        f"| Macro AUROC (unweighted mean of events) | {r(summary.get('macro_auroc_unweighted_events'))} |",
        "",
        "## By event",
        "",
        "| Event | Chips | Flood pixels | Dry land pixels | AUROC | AP |",
        "|-------|------:|-------------:|----------------:|------:|---:|",
    ]
    for e in summary["by_event"]:
        lines.append(
            f"| {e['event']} | {e['n_chips']} | {e['n_pos']} | {e['n_neg']} | {r(e['auroc'])} | {r(e['average_precision'])} |"
        )
    b = summary["bytes"]
    lines += [
        "",
        "## Bytes",
        "",
        f"- Sen1Floods11 files downloaded this run: {b['sen1floods11_downloaded_bytes']} bytes",
        f"- Sen1Floods11 cache hits: {b['sen1floods11_cache_hit_bytes']} bytes",
        f"- Copernicus COG HTTP GET bodies: {b['copernicus_http_get_bytes']} bytes",
        f"- Largest Copernicus object Content-Length (not fully downloaded): {b['copernicus_largest_head_content_length']} bytes",
        "",
    ]
    return "\n".join(lines) + "\n"
