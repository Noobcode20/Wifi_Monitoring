#!/usr/bin/env python3
"""Offline diagnostic for Zenodo DP_LOS image labels; never used by live CSI.

Dataset: https://zenodo.org/records/8021099
The public files are 400x52-pixel grayscale CSI amplitude spectrogram PNGs
(400 temporal samples by 52 subcarrier rows). The feature path transposes the
native image array to time-by-subcarrier order, averages 16 contiguous
subcarrier bands, and applies train-only standardization plus nearest
centroids. It trains on trainLabels.csv and reports the supplied
validation/test splits. The images are not raw CSI frames and results do not
measure transfer to PresenceEngine.

Requires Pillow and NumPy. It reads the dataset in place and writes no files.
"""

import argparse
import csv
from pathlib import Path

import numpy as np
from PIL import Image


TIME_SAMPLES, SUBCARRIERS, BANDS = 400, 52, 16
SPLITS = ("train", "validation", "test")
FEATURES = ("raw-band-means", "rms-normalized-means", "rms-normalized-stats")


def read_labels(root, split):
    path = root / f"{split}Labels.csv"
    labels = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            if len(row) != 2:
                raise ValueError(f"{path}: expected image_index,class rows")
            index, category = map(int, row)
            if index < 0 or not 0 <= category <= 5 or index in labels:
                raise ValueError(f"{path}: invalid or duplicate label row {row!r}")
            labels[index] = int(category > 0)
    if not labels:
        raise ValueError(f"{path}: no labels")
    return labels


def image_band_series(path):
    with Image.open(path) as image:
        # PNG width is time and height is subcarrier, so transpose to time rows.
        pixels = np.asarray(image.convert("L"), dtype=np.float64).T / 255.0
    if pixels.shape != (TIME_SAMPLES, SUBCARRIERS):
        raise ValueError(f"{path}: expected grayscale image {TIME_SAMPLES}x{SUBCARRIERS}, got {pixels.shape}")
    return np.stack([
        pixels[:, k * SUBCARRIERS // BANDS:(k + 1) * SUBCARRIERS // BANDS].mean(axis=1)
        for k in range(BANDS)
    ], axis=1)


def image_features(path, feature_name):
    series = image_band_series(path)
    if feature_name == "raw-band-means":
        return series.mean(axis=0)
    normalized = series / (np.sqrt(np.mean(series ** 2, axis=1, keepdims=True)) + 1e-9)
    if feature_name == "rms-normalized-means":
        return normalized.mean(axis=0)
    if feature_name == "rms-normalized-stats":
        return np.concatenate((normalized.mean(axis=0), normalized.std(axis=0),
                               np.abs(np.diff(normalized, axis=0)).mean(axis=0)))
    raise ValueError(f"unsupported feature path: {feature_name}")


def load_split(root, split, feature_name):
    labels = read_labels(root, split)
    rows = []
    for index in sorted(labels):
        rows.append(image_features(root / f"{index}.png", feature_name))
    return np.stack(rows), np.array([labels[index] for index in sorted(labels)], dtype=np.int8)


def fit_centroids(train_x, train_y):
    mean = train_x.mean(axis=0)
    scale = train_x.std(axis=0)
    scale[scale < 1e-8] = 1.0
    standardized = (train_x - mean) / scale
    centers = np.stack([standardized[train_y == label].mean(axis=0) for label in (0, 1)])
    return mean, scale, centers


def predict(x, model):
    mean, scale, centers = model
    standardized = (x - mean) / scale
    distances = ((standardized[:, None, :] - centers[None, :, :]) ** 2).mean(axis=2)
    return distances.argmin(axis=1)


def summarize(y_true, y_pred):
    tn = int(np.sum((y_true == 0) & (y_pred == 0)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    recall_empty = tn / max(tn + fp, 1)
    recall_present = tp / max(tp + fn, 1)
    return {"n": len(y_true), "no_presence": int(np.sum(y_true == 0)),
            "presence": int(np.sum(y_true == 1)), "accuracy": (tn + tp) / max(len(y_true), 1),
            "balanced_accuracy": (recall_empty + recall_present) / 2,
            "tn": tn, "fp": fp, "fn": fn, "tp": tp}


def evaluate_domain(root):
    splits = {name: read_labels(root, name) for name in SPLITS}
    all_indices = [set(labels) for labels in splits.values()]
    if any(all_indices[i] & all_indices[j] for i in range(3) for j in range(i + 1, 3)):
        raise ValueError("official train, validation, and test indices overlap")
    output = {}
    for feature_name in FEATURES:
        train_x, train_y = load_split(root, "train", feature_name)
        model = fit_centroids(train_x, train_y)
        output[feature_name] = {"train_count": len(train_y), "splits": {}}
        for split in ("validation", "test"):
            x, y = load_split(root, split, feature_name)
            output[feature_name]["splits"][split] = summarize(y, predict(x, model))
    return output


def evaluate_domains(los_root, nlos_root):
    domains = {"LOS": los_root, "NLOS": nlos_root}
    per_domain = {name: evaluate_domain(root) for name, root in domains.items()}
    cross_domain = {}
    for feature_name in FEATURES:
        cross_domain[feature_name] = {}
        for train_domain, test_domain in (("LOS", "NLOS"), ("NLOS", "LOS")):
            train_x, train_y = load_split(domains[train_domain], "train", feature_name)
            test_x, test_y = load_split(domains[test_domain], "test", feature_name)
            model = fit_centroids(train_x, train_y)
            cross_domain[feature_name][f"{train_domain}_to_{test_domain}"] = summarize(test_y, predict(test_x, model))
    return per_domain, cross_domain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--los", required=True, type=Path, help="DP_LOS directory with PNGs and official split CSVs")
    parser.add_argument("--nlos", required=True, type=Path, help="DP_NLOS directory with PNGs and official split CSVs")
    args = parser.parse_args()
    los_root, nlos_root = args.los.expanduser().resolve(), args.nlos.expanduser().resolve()
    per_domain, cross_domain = evaluate_domains(los_root, nlos_root)
    print("Dataset: Zenodo 8021099 DP_LOS and DP_NLOS; binary mapping class 0=no presence, classes 1-5=presence")
    print("Features use 16 subcarrier bands across image time samples; they remain image-derived, not raw live CSI frames.")
    for domain, results in per_domain.items():
        print(f"\n{domain} within-domain official split evaluation")
        for feature_name, result in results.items():
            print(f"  {feature_name} (train n={result['train_count']})")
            for split, metrics in result["splits"].items():
                print(f"    {split}: n={metrics['n']} no_presence={metrics['no_presence']} presence={metrics['presence']} "
                      f"accuracy={metrics['accuracy']:.3f} balanced_accuracy={metrics['balanced_accuracy']:.3f} "
                      f"TN={metrics['tn']} FP={metrics['fp']} FN={metrics['fn']} TP={metrics['tp']}")
    print("\nCross-domain transfer (train on one domain's official training split; test on the other's official test split)")
    for feature_name, directions in cross_domain.items():
        print(f"  {feature_name}")
        for direction, metrics in directions.items():
            print(f"    {direction}: n={metrics['n']} no_presence={metrics['no_presence']} presence={metrics['presence']} "
                  f"accuracy={metrics['accuracy']:.3f} balanced_accuracy={metrics['balanced_accuracy']:.3f} "
                  f"TN={metrics['tn']} FP={metrics['fp']} FN={metrics['fn']} TP={metrics['tp']}")
    print("\nInterpretation: offline image-derived diagnostics only; results do not estimate ESP32 live-runtime accuracy.")


if __name__ == "__main__":
    main()
