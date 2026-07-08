# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageGrab

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:
    tk = None
    messagebox = None
    ttk = None

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}
APP_ICON_NAME = "scan_icon.ico"
APP_ICON_PNG_NAME = "scan_icon.png"
APP_USER_MODEL_ID = "Codex.Scan.MysteriousExaminer.StarIcon"
DIAGNOSTIC_SAVE_INTERVAL_SECONDS = 10.0
DIAGNOSTIC_MAX_RECORDS = 30


@dataclass
class TemplateHit:
    target_id: str
    target_name: str
    template: Path
    method: str
    score: float
    x: int
    y: int
    width: int
    height: int
    threshold: float

    @property
    def found(self) -> bool:
        return self.score >= self.threshold

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "target_id": self.target_id,
            "name": self.target_name,
            "template": self.template.name,
            "method": self.method,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "x": self.x,
            "y": self.y,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class PreparedTemplate:
    path: Path
    method: str
    kernel: np.ndarray
    energy: float
    width: int
    height: int


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base / path


def runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(filename: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / filename
        if bundled.exists():
            return bundled
    return runtime_base_dir() / filename


def set_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def apply_app_icon(root: Any) -> None:
    icon_path = resource_path(APP_ICON_NAME)
    if icon_path.exists():
        try:
            root.iconbitmap(default=str(icon_path))
        except Exception:
            pass

    png_path = resource_path(APP_ICON_PNG_NAME)
    if tk is not None and png_path.exists():
        try:
            icon_photo = tk.PhotoImage(file=str(png_path))
            root.iconphoto(True, icon_photo)
            root._scan_app_icon_photo = icon_photo
        except Exception:
            pass


def image_to_gray(path: Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return pil_to_gray(img)


def pil_to_gray(img: Image.Image) -> np.ndarray:
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114


def gradient_image(gray: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(gray, dtype=np.float32)
    gy = np.zeros_like(gray, dtype=np.float32)
    gx[:, 1:-1] = (gray[:, 2:] - gray[:, :-2]) * 0.5
    gy[1:-1, :] = (gray[2:, :] - gray[:-2, :]) * 0.5
    mag = np.sqrt(gx * gx + gy * gy)
    peak = float(mag.max())
    if peak > 0:
        mag /= peak
    return mag


def window_sum(image: np.ndarray, height: int, width: int) -> np.ndarray:
    integral = np.pad(image, ((1, 0), (1, 0)), mode="constant")
    integral = integral.cumsum(axis=0).cumsum(axis=1)
    return (
        integral[height:, width:]
        - integral[:-height, width:]
        - integral[height:, :-width]
        + integral[:-height, :-width]
    )


def fft_convolve_valid(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    image_height, image_width = image.shape
    kernel_height, kernel_width = kernel.shape
    output_shape = (image_height + kernel_height - 1, image_width + kernel_width - 1)

    image_fft = np.fft.rfft2(image, output_shape)
    kernel_fft = np.fft.rfft2(kernel, output_shape)
    full = np.fft.irfft2(image_fft * kernel_fft, output_shape)
    return full[kernel_height - 1 : image_height, kernel_width - 1 : image_width]


def normalized_cross_correlation(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    template_height, template_width = template.shape
    image_height, image_width = image.shape
    if template_height > image_height or template_width > image_width:
        raise ValueError("Template is larger than image.")

    template_centered = template - float(template.mean())
    template_energy = float(np.sqrt(np.sum(template_centered * template_centered)))
    if template_energy <= 1e-8:
        raise ValueError("Template has too little detail.")

    numerator = fft_convolve_valid(image, template_centered[::-1, ::-1])

    area = template_height * template_width
    sum_image = window_sum(image, template_height, template_width)
    sum_image_sq = window_sum(image * image, template_height, template_width)
    window_energy_sq = sum_image_sq - (sum_image * sum_image / area)
    window_energy_sq = np.maximum(window_energy_sq, 0)
    denominator = np.sqrt(window_energy_sq) * template_energy

    with np.errstate(divide="ignore", invalid="ignore"):
        score = numerator / denominator
    score[~np.isfinite(score)] = -1.0
    return np.clip(score, -1.0, 1.0)


def prepare_template_for_method(template_path: Path, method: str) -> PreparedTemplate | None:
    template_gray = image_to_gray(template_path)
    if method == "edge":
        prepared_image = gradient_image(template_gray)
    elif method == "gray":
        prepared_image = template_gray
    else:
        raise ValueError(f"Unknown method: {method}")

    centered = prepared_image - float(prepared_image.mean())
    energy = float(np.sqrt(np.sum(centered * centered)))
    if energy <= 1e-8:
        return None

    height, width = prepared_image.shape
    return PreparedTemplate(
        path=template_path,
        method=method,
        kernel=centered[::-1, ::-1],
        energy=energy,
        width=int(width),
        height=int(height),
    )


def normalized_cross_correlation_prepared(image: np.ndarray, template: PreparedTemplate) -> np.ndarray:
    image_height, image_width = image.shape
    if template.height > image_height or template.width > image_width:
        raise ValueError("Template is larger than image.")

    numerator = fft_convolve_valid(image, template.kernel)

    area = template.height * template.width
    sum_image = window_sum(image, template.height, template.width)
    sum_image_sq = window_sum(image * image, template.height, template.width)
    window_energy_sq = sum_image_sq - (sum_image * sum_image / area)
    window_energy_sq = np.maximum(window_energy_sq, 0)
    denominator = np.sqrt(window_energy_sq) * template.energy

    with np.errstate(divide="ignore", invalid="ignore"):
        score = numerator / denominator
    score[~np.isfinite(score)] = -1.0
    return np.clip(score, -1.0, 1.0)


def best_match_for_method(image: np.ndarray, template: np.ndarray, method: str) -> tuple[float, int, int]:
    if method == "gray":
        score_map = normalized_cross_correlation(image, template)
    elif method == "edge":
        score_map = normalized_cross_correlation(gradient_image(image), gradient_image(template))
    else:
        raise ValueError(f"Unknown method: {method}")

    index = int(np.argmax(score_map))
    y, x = np.unravel_index(index, score_map.shape)
    return float(score_map[y, x]), int(x), int(y)


def best_match_for_prepared(image: np.ndarray, template: PreparedTemplate) -> tuple[float, int, int]:
    score_map = normalized_cross_correlation_prepared(image, template)
    index = int(np.argmax(score_map))
    y, x = np.unravel_index(index, score_map.shape)
    return float(score_map[y, x]), int(x), int(y)


def template_files(template_dir: Path) -> list[Path]:
    if not template_dir.exists():
        return []
    return sorted(
        path for path in template_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def crop_search_region(screen_gray: np.ndarray, target: dict[str, Any]) -> tuple[np.ndarray, int, int]:
    region = target.get("search_region")
    if not region:
        return screen_gray, 0, 0

    if len(region) != 4:
        raise ValueError("search_region must be [x, y, width, height].")

    x, y, width, height = [int(value) for value in region]
    image_height, image_width = screen_gray.shape
    x = max(0, min(x, image_width - 1))
    y = max(0, min(y, image_height - 1))
    right = max(x + 1, min(x + width, image_width))
    bottom = max(y + 1, min(y + height, image_height))
    return screen_gray[y:bottom, x:right], x, y


def scan_target(image_path: Path, target: dict[str, Any], base_dir: Path) -> TemplateHit | None:
    target_id = str(target["id"])
    target_name = str(target.get("name", target_id))
    threshold = float(target.get("threshold", 0.7))
    requested_method = str(target.get("method", "edge_gray"))
    methods = ["edge", "gray"] if requested_method == "edge_gray" else [requested_method]

    full_screen_gray = image_to_gray(image_path)
    screen_gray, offset_x, offset_y = crop_search_region(full_screen_gray, target)
    template_dir = resolve_path(base_dir, target["template_dir"])
    best: TemplateHit | None = None

    for template_path in template_files(template_dir):
        template_gray = image_to_gray(template_path)
        height, width = template_gray.shape
        if height > screen_gray.shape[0] or width > screen_gray.shape[1]:
            continue

        for method in methods:
            try:
                score, x, y = best_match_for_method(screen_gray, template_gray, method)
            except ValueError:
                continue

            candidate = TemplateHit(
                target_id=target_id,
                target_name=target_name,
                template=template_path,
                method=method,
                score=score,
                x=x + offset_x,
                y=y + offset_y,
                width=width,
                height=height,
                threshold=threshold,
            )
            if best is None or candidate.score > best.score:
                best = candidate

    return best


def requested_methods(target: dict[str, Any]) -> list[str]:
    requested_method = str(target.get("method", "edge_gray"))
    return ["edge", "gray"] if requested_method == "edge_gray" else [requested_method]


class TemplateCache:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._cache: dict[tuple[Path, str], list[PreparedTemplate]] = {}

    def get(self, template_dir_value: str | Path, method: str) -> list[PreparedTemplate]:
        template_dir = resolve_path(self.base_dir, template_dir_value)
        key = (template_dir, method)
        if key not in self._cache:
            prepared: list[PreparedTemplate] = []
            for template_path in template_files(template_dir):
                item = prepare_template_for_method(template_path, method)
                if item is not None:
                    prepared.append(item)
            self._cache[key] = prepared
        return self._cache[key]


def scan_prepared_target(
    full_screen_gray: np.ndarray,
    target: dict[str, Any],
    base_dir: Path,
    cache: TemplateCache,
) -> TemplateHit | None:
    target_id = str(target["id"])
    target_name = str(target.get("name", target_id))
    threshold = float(target.get("threshold", 0.7))
    methods = requested_methods(target)

    screen_gray, offset_x, offset_y = crop_search_region(full_screen_gray, target)
    method_images: dict[str, np.ndarray] = {}
    best: TemplateHit | None = None

    for method in methods:
        if method == "edge":
            method_images[method] = gradient_image(screen_gray)
        elif method == "gray":
            method_images[method] = screen_gray
        else:
            raise ValueError(f"Unknown method: {method}")

        for prepared in cache.get(target["template_dir"], method):
            if prepared.height > method_images[method].shape[0] or prepared.width > method_images[method].shape[1]:
                continue
            try:
                score, x, y = best_match_for_prepared(method_images[method], prepared)
            except ValueError:
                continue

            candidate = TemplateHit(
                target_id=target_id,
                target_name=target_name,
                template=prepared.path,
                method=method,
                score=score,
                x=x + offset_x,
                y=y + offset_y,
                width=prepared.width,
                height=prepared.height,
                threshold=threshold,
            )
            if best is None or candidate.score > best.score:
                best = candidate

    return best


class PreparedScanner:
    def __init__(self, config_path: Path, target_id: str | None) -> None:
        self.config_path = config_path
        self.base_dir = config_path.parent
        self.config = load_config(config_path)
        self.targets = enabled_targets(self.config, target_id)
        if not self.targets:
            raise SystemExit("找不到啟用中的目標，請檢查 config.json。")
        self.cache = TemplateCache(self.base_dir)

    def exclusion_hit_for_target(
        self,
        full_screen_gray: np.ndarray,
        target: dict[str, Any],
        candidate: TemplateHit,
    ) -> TemplateHit | None:
        exclude_dirs = target.get("exclude_template_dirs", [])
        if isinstance(exclude_dirs, str):
            exclude_dirs = [exclude_dirs]
        if not exclude_dirs:
            return None

        threshold = float(target.get("exclude_threshold", target.get("threshold", 0.7)))
        min_overlap = float(target.get("exclude_min_overlap", 0.25))
        method = str(target.get("exclude_method", target.get("method", "edge_gray")))

        best_exclusion: TemplateHit | None = None
        for index, template_dir in enumerate(exclude_dirs):
            exclusion_target: dict[str, Any] = {
                "id": f"{target.get('id', 'target')}:exclude:{index}",
                "name": f"exclude:{Path(str(template_dir)).name}",
                "template_dir": template_dir,
                "enabled": True,
                "threshold": threshold,
                "method": method,
            }
            if "search_region" in target:
                exclusion_target["search_region"] = target["search_region"]

            hit = scan_prepared_target(full_screen_gray, exclusion_target, self.base_dir, self.cache)
            if hit is None or not hit.found:
                continue
            if overlap_ratio(candidate, hit) < min_overlap:
                continue
            if best_exclusion is None or hit.score > best_exclusion.score:
                best_exclusion = hit

        return best_exclusion

    def scan_gray(self, full_screen_gray: np.ndarray, image_label: str) -> dict[str, Any]:
        hits: list[TemplateHit] = []
        excluded_hits: list[dict[str, Any]] = []

        for target in self.targets:
            hit = scan_prepared_target(full_screen_gray, target, self.base_dir, self.cache)
            if hit is None:
                continue

            exclusion = self.exclusion_hit_for_target(full_screen_gray, target, hit)
            if hit.found and exclusion is not None:
                excluded_hits.append({
                    "target": hit.as_dict(),
                    "excluded_by": exclusion.as_dict(),
                    "overlap": round(overlap_ratio(hit, exclusion), 4),
                })
                continue

            hits.append(hit)

        best_hit = max(hits, key=lambda h: h.score, default=None)
        return {
            "image": image_label,
            "found": bool(best_hit and best_hit.found),
            "best": best_hit.as_dict() if best_hit else None,
            "excluded": excluded_hits,
            "debug_image": None,
        }

    def scan_pil(self, image: Image.Image, image_label: str) -> dict[str, Any]:
        return self.scan_gray(pil_to_gray(image.convert("RGB")), image_label)


def overlap_ratio(first: TemplateHit, second: TemplateHit) -> float:
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    if right <= left or bottom <= top:
        return 0.0

    intersection = float((right - left) * (bottom - top))
    first_area = float(first.width * first.height)
    second_area = float(second.width * second.height)
    return intersection / max(1.0, min(first_area, second_area))


def exclusion_hit_for_target(
    image_path: Path,
    target: dict[str, Any],
    base_dir: Path,
    candidate: TemplateHit,
) -> TemplateHit | None:
    exclude_dirs = target.get("exclude_template_dirs", [])
    if isinstance(exclude_dirs, str):
        exclude_dirs = [exclude_dirs]
    if not exclude_dirs:
        return None

    threshold = float(target.get("exclude_threshold", target.get("threshold", 0.7)))
    min_overlap = float(target.get("exclude_min_overlap", 0.25))
    method = str(target.get("exclude_method", target.get("method", "edge_gray")))

    best_exclusion: TemplateHit | None = None
    for index, template_dir in enumerate(exclude_dirs):
        exclusion_target: dict[str, Any] = {
            "id": f"{target.get('id', 'target')}:exclude:{index}",
            "name": f"exclude:{Path(str(template_dir)).name}",
            "template_dir": template_dir,
            "enabled": True,
            "threshold": threshold,
            "method": method,
        }
        if "search_region" in target:
            exclusion_target["search_region"] = target["search_region"]

        hit = scan_target(image_path, exclusion_target, base_dir)
        if hit is None or not hit.found:
            continue
        if overlap_ratio(candidate, hit) < min_overlap:
            continue
        if best_exclusion is None or hit.score > best_exclusion.score:
            best_exclusion = hit

    return best_exclusion


def draw_debug(image_path: Path, hit: TemplateHit | None, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"scan_{timestamp}_{image_path.stem}.png"

    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    if hit is not None and hit.found:
        x1, y1 = hit.x, hit.y
        x2, y2 = hit.x + hit.width, hit.y + hit.height
        draw.rectangle((x1, y1, x2, y2), outline=(255, 0, 0), width=3)
        label = f"{hit.target_id} {hit.score:.3f} ({hit.center_x},{hit.center_y})"
        draw.rectangle((x1, max(0, y1 - 16), x1 + 230, y1), fill=(255, 0, 0))
        draw.text((x1 + 4, max(0, y1 - 15)), label, fill=(255, 255, 255))
    else:
        draw.rectangle((8, 8, 260, 34), fill=(0, 0, 0))
        draw.text((16, 16), "not found", fill=(255, 255, 255))

    img.save(output_path)
    return output_path


def diagnostic_hint(result: dict[str, Any]) -> str:
    best = result.get("best")
    if not best:
        return "沒有有效模板比對結果；可能是模板資料缺漏、畫面太小或搜尋區域不正確。"

    try:
        score = float(best.get("score", 0.0))
        threshold = float(best.get("threshold", 0.7))
    except Exception:
        return "最高分數資料無法判讀；請查看診斷圖確認畫面是否被遮蔽。"

    if score >= threshold * 0.85:
        return "最高分數接近門檻；可能是角度、遮蔽、名字露出不完整，建議補一張正式模板。"
    if score >= threshold * 0.6:
        return "有相似特徵但不足；可能是不同外觀、背景干擾或模板不夠完整。"
    return "分數差距較大；可能 NPC 不在畫面、視窗被遮蔽、畫面比例不同或需要新增模板。"


def draw_live_diagnostic(image: Image.Image, result: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = image.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    best = result.get("best")

    if best:
        x = int(best.get("x", 0))
        y = int(best.get("y", 0))
        width = int(best.get("width", 0))
        height = int(best.get("height", 0))
        score = best.get("score", "--")
        threshold = best.get("threshold", "--")
        template = best.get("template", "--")
        method = best.get("method", "--")
        draw.rectangle((x, y, x + width, y + height), outline="yellow", width=3)
        draw.rectangle((8, 8, 430, 50), fill=(0, 0, 0))
        draw.text((16, 16), f"not found  score={score} / threshold={threshold}", fill=(255, 255, 0))
        draw.text((16, 32), f"best={template}  method={method}", fill=(255, 255, 0))
    else:
        draw.rectangle((8, 8, 260, 34), fill=(0, 0, 0))
        draw.text((16, 16), "not found  no template match", fill=(255, 255, 0))

    img.save(output_path)


def save_not_found_diagnostic(
    image: Image.Image,
    result: dict[str, Any],
    output_dir: Path,
    hwnd: int,
    title: str,
    bbox: tuple[int, int, int, int],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"not_found_{timestamp}_hwnd_{hwnd}"
    image_path = output_dir / f"{base_name}.png"
    meta_path = output_dir / f"{base_name}.json"
    hint = diagnostic_hint(result)

    draw_live_diagnostic(image, result, image_path)
    metadata = {
        "timestamp": timestamp,
        "hwnd": hwnd,
        "title": title,
        "bbox": list(bbox),
        "found": result.get("found"),
        "best": result.get("best"),
        "excluded": result.get("excluded", []),
        "hint": hint,
        "image": str(image_path),
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    result["diagnostic_image"] = str(image_path)
    result["diagnostic_meta"] = str(meta_path)
    result["diagnostic_hint"] = hint
    prune_diagnostics(output_dir, DIAGNOSTIC_MAX_RECORDS)
    return image_path, meta_path


def prune_diagnostics(output_dir: Path, max_records: int) -> None:
    if max_records <= 0:
        return
    try:
        meta_files = sorted(
            output_dir.glob("not_found_*_hwnd_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return

    for meta_path in meta_files[max_records:]:
        image_path = meta_path.with_suffix(".png")
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            stored_image = metadata.get("image")
            if stored_image:
                image_path = Path(stored_image)
        except Exception:
            pass
        for path in (image_path, meta_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def enabled_targets(config: dict[str, Any], target_id: str | None) -> list[dict[str, Any]]:
    targets = [t for t in config.get("targets", []) if t.get("enabled", True)]
    if target_id:
        targets = [t for t in targets if t.get("id") == target_id]
    return targets


def scan_image(config_path: Path, image_path: Path, target_id: str | None, save_debug: bool) -> dict[str, Any]:
    base_dir = config_path.parent
    config = load_config(config_path)
    targets = enabled_targets(config, target_id)
    if not targets:
        raise SystemExit("找不到啟用中的目標，請檢查 config.json。")

    hits: list[TemplateHit] = []
    excluded_hits: list[dict[str, Any]] = []
    for target in targets:
        hit = scan_target(image_path, target, base_dir)
        if hit is None:
            continue

        exclusion = exclusion_hit_for_target(image_path, target, base_dir, hit)
        if hit.found and exclusion is not None:
            excluded_hits.append({
                "target": hit.as_dict(),
                "excluded_by": exclusion.as_dict(),
                "overlap": round(overlap_ratio(hit, exclusion), 4),
            })
            continue

        hits.append(hit)

    best_hit = max(hits, key=lambda h: h.score, default=None)

    debug_path: Path | None = None
    if save_debug:
        debug_dir = resolve_path(base_dir, config.get("debug", {}).get("output_dir", "results"))
        debug_path = draw_debug(image_path, best_hit, debug_dir)

    return {
        "image": str(image_path),
        "found": bool(best_hit and best_hit.found),
        "best": best_hit.as_dict() if best_hit else None,
        "excluded": excluded_hits,
        "debug_image": str(debug_path) if debug_path else None,
    }


def print_human(result: dict[str, Any]) -> None:
    best = result.get("best")
    if result["found"] and best:
        print(f"找到：{best['name']}")
        print(f"位置：X={best['center_x']} Y={best['center_y']}")
        print(f"分數：{best['score']} / 門檻 {best['threshold']}")
        print(f"模板：{best['template']} / 方法：{best['method']}")
    else:
        if best:
            print("目前畫面未出現指定目標")
            print(f"最高分數：{best['score']} / 門檻 {best['threshold']}")
            print(f"最高模板：{best['template']} / 方法：{best['method']}")
        else:
            print("目前畫面未出現指定目標，也沒有可用模板。")
    if result.get("excluded"):
        print(f"已排除疑似固定 NPC：{len(result['excluded'])} 個")
    if result.get("debug_image"):
        print(f"Debug圖：{result['debug_image']}")


def self_test(config_path: Path, save_debug: bool) -> None:
    base_dir = config_path.parent
    samples_dir = base_dir / "samples"
    samples = sorted(path for path in samples_dir.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)
    if not samples:
        raise SystemExit("samples 資料夾沒有測試圖片。")

    for sample in samples:
        print(f"\n=== {sample.name} ===")
        result = scan_image(config_path, sample, None, save_debug)
        print_human(result)


def require_windows() -> None:
    if sys.platform != "win32":
        raise SystemExit("即時視窗掃描目前只支援 Windows。")


def enable_dpi_awareness() -> None:
    require_windows()
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def visible_windows() -> list[tuple[int, str]]:
    require_windows()
    user32 = ctypes.windll.user32
    windows: list[tuple[int, str]] = []

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def enum_proc(hwnd: int, lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            title = window_title(hwnd)
            if title.strip():
                windows.append((int(hwnd), title))
        return True

    user32.EnumWindows(enum_proc_type(enum_proc), 0)
    return windows


def print_window_list() -> None:
    enable_dpi_awareness()
    for hwnd, title in visible_windows():
        try:
            left, top, right, bottom = client_bbox(hwnd)
            size = f"{right - left}x{bottom - top}+{left}+{top}"
        except Exception:
            size = "unknown"
        print(f"{hwnd}: {title} [{size}]")


def find_window_by_title(title_part: str) -> tuple[int, str]:
    needle = title_part.casefold()
    matches = [(hwnd, title) for hwnd, title in visible_windows() if needle in title.casefold()]
    if not matches:
        raise SystemExit(f"找不到包含這段標題的視窗：{title_part}")
    return matches[0]


def find_windows_by_title(title_part: str) -> list[tuple[int, str]]:
    needle = title_part.casefold()
    matches = [(hwnd, title) for hwnd, title in visible_windows() if needle in title.casefold()]
    if not matches:
        raise SystemExit(f"找不到包含這段標題的視窗：{title_part}")
    return matches


def parse_window_handles(value: str) -> list[int]:
    handles: list[int] = []
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        item = part.strip()
        if not item:
            continue
        handles.append(int(item, 0))
    if not handles:
        raise SystemExit("請在 --window-handles 後面指定至少一個視窗編號。")
    return handles


def foreground_window_after_delay(delay_seconds: float) -> tuple[int, str]:
    user32 = ctypes.windll.user32
    seconds = max(0.0, delay_seconds)
    if seconds > 0:
        print(f"請在 {seconds:.1f} 秒內點選要掃描的遊戲視窗...")
        time.sleep(seconds)
    hwnd = int(user32.GetForegroundWindow())
    title = window_title(hwnd)
    if not hwnd or not title:
        raise SystemExit("無法取得目前前景視窗，請改用 --window-title。")
    return hwnd, title


def client_bbox(hwnd: int) -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("無法讀取視窗 client 區域。")

    top_left = wintypes.POINT(rect.left, rect.top)
    bottom_right = wintypes.POINT(rect.right, rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
        raise RuntimeError("無法換算視窗左上座標。")
    if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        raise RuntimeError("無法換算視窗右下座標。")

    left, top = int(top_left.x), int(top_left.y)
    right, bottom = int(bottom_right.x), int(bottom_right.y)
    if right <= left or bottom <= top:
        raise RuntimeError("視窗擷取範圍無效，請確認視窗沒有最小化。")
    return left, top, right, bottom


def select_watch_window(args: argparse.Namespace) -> tuple[int, str]:
    enable_dpi_awareness()
    if args.window_handle:
        hwnd = int(str(args.window_handle), 0)
        title = window_title(hwnd)
        if not title:
            raise SystemExit(f"找不到這個視窗編號：{args.window_handle}")
        return hwnd, title
    if args.window_title:
        return find_window_by_title(args.window_title)
    return foreground_window_after_delay(args.focus_delay)


def select_watch_windows(args: argparse.Namespace) -> list[tuple[int, str]]:
    enable_dpi_awareness()
    if args.window_handles:
        windows: list[tuple[int, str]] = []
        for hwnd in parse_window_handles(args.window_handles):
            title = window_title(hwnd)
            if not title:
                raise SystemExit(f"找不到這個視窗編號：{hwnd}")
            windows.append((hwnd, title))
        return windows

    if args.multi_watch and args.window_title:
        return find_windows_by_title(args.window_title)

    return [select_watch_window(args)]


def bring_window_to_front(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 5)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)


class RedFrameManager:
    def __init__(self, enabled: bool, thickness: int, seconds: float, root: Any | None = None) -> None:
        self.enabled = enabled and tk is not None
        self.thickness = max(2, int(thickness))
        self.seconds = max(0.5, float(seconds))
        self.root: Any | None = root
        self.owns_root = False
        self.frames: dict[int, tuple[list[Any], float]] = {}
        if enabled and tk is None:
            print("紅框提示不可用：此環境沒有 tkinter，會改用文字與提示音。", file=sys.stderr)

    def _ensure_root(self) -> Any | None:
        if not self.enabled:
            return None
        if self.root is None:
            set_app_user_model_id()
            self.root = tk.Tk()
            apply_app_icon(self.root)
            self.root.withdraw()
            self.owns_root = True
        return self.root

    def _make_bar(self, x: int, y: int, width: int, height: int) -> Any:
        root = self._ensure_root()
        window = tk.Toplevel(root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg="red")
        window.geometry(f"{max(1, width)}x{max(1, height)}+{x}+{y}")
        return window

    def show(self, key: int, bbox: tuple[int, int, int, int]) -> None:
        if not self.enabled:
            return
        self.hide(key)
        left, top, right, bottom = bbox
        width = right - left
        height = bottom - top
        thickness = self.thickness
        bars = [
            self._make_bar(left - thickness, top - thickness, width + thickness * 2, thickness),
            self._make_bar(left - thickness, bottom, width + thickness * 2, thickness),
            self._make_bar(left - thickness, top, thickness, height),
            self._make_bar(right, top, thickness, height),
        ]
        self.frames[key] = (bars, time.monotonic() + self.seconds)
        self.pump()

    def hide(self, key: int) -> None:
        item = self.frames.pop(key, None)
        if not item:
            return
        for window in item[0]:
            try:
                window.destroy()
            except Exception:
                pass

    def cleanup(self) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        for key, item in list(self.frames.items()):
            if item[1] <= now:
                self.hide(key)
        self.pump()

    def pump(self) -> None:
        if self.root is None:
            return
        try:
            self.root.update_idletasks()
            if self.owns_root:
                self.root.update()
        except Exception:
            pass

    def destroy(self) -> None:
        for key in list(self.frames):
            self.hide(key)
        if self.root is not None and self.owns_root:
            try:
                self.root.destroy()
            except Exception:
                pass
        self.root = None
        self.owns_root = False


def capture_window(hwnd: int) -> Image.Image:
    bbox = client_bbox(hwnd)
    return ImageGrab.grab(bbox=bbox).convert("RGB")


def print_watch_result(
    result: dict[str, Any],
    bbox: tuple[int, int, int, int],
    show_not_found: bool,
    window_label: str,
) -> bool:
    timestamp = time.strftime("%H:%M:%S")
    best = result.get("best")
    if result.get("found") and best:
        screen_x = bbox[0] + int(best["center_x"])
        screen_y = bbox[1] + int(best["center_y"])
        print(
            f"[{timestamp}] [{window_label}] 找到：{best['name']} "
            f"畫面X={best['center_x']} 畫面Y={best['center_y']} "
            f"螢幕X={screen_x} 螢幕Y={screen_y} "
            f"分數={best['score']}"
        )
        return True

    if show_not_found:
        if best:
            print(f"[{timestamp}] [{window_label}] 未出現，最高分數={best['score']} / 門檻={best['threshold']}")
        else:
            print(f"[{timestamp}] [{window_label}] 未出現，沒有可用模板。")
    return False


def watch_window(config_path: Path, args: argparse.Namespace) -> int:
    windows = select_watch_windows(args)
    if args.bring_to_front and len(windows) == 1:
        bring_window_to_front(windows[0][0])

    print("開始即時掃描視窗：")
    for index, (hwnd, title) in enumerate(windows, start=1):
        try:
            left, top, right, bottom = client_bbox(hwnd)
            size = f"{right - left}x{bottom - top}+{left}+{top}"
        except Exception:
            size = "unknown"
        print(f"  {index}. {hwnd}: {title} [{size}]")
    print("按 Ctrl+C 停止。")

    base_dir = config_path.parent
    capture_path = resolve_path(base_dir, args.live_capture)
    if args.save_live_capture:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
    interval = max(0.1, float(args.interval))
    consecutive_errors = 0
    scanner = PreparedScanner(config_path, args.target)
    red_frames = RedFrameManager(
        enabled=not args.no_red_frame,
        thickness=args.red_frame_thickness,
        seconds=args.red_frame_seconds,
    )

    try:
        while True:
            any_found = False
            for index, (hwnd, title) in enumerate(windows, start=1):
                label = f"{index}/{len(windows)} hwnd={hwnd}"
                try:
                    bbox = client_bbox(hwnd)
                    image = ImageGrab.grab(bbox=bbox).convert("RGB")
                    if args.save_live_capture:
                        current_capture = capture_path
                        if len(windows) > 1:
                            current_capture = capture_path.with_name(f"{capture_path.stem}_{hwnd}{capture_path.suffix}")
                        image.save(current_capture)
                    result = scanner.scan_pil(image, f"hwnd={hwnd}")
                    found = print_watch_result(result, bbox, args.show_not_found, label)
                    if found:
                        any_found = True
                        red_frames.show(hwnd, bbox)
                        if args.beep:
                            try:
                                import winsound

                                winsound.Beep(1200, 180)
                            except Exception:
                                pass
                except Exception as exc:
                    consecutive_errors += 1
                    print(f"[{label}] 掃描失敗：{exc}", file=sys.stderr)
                    if consecutive_errors >= 3:
                        raise
            red_frames.cleanup()
            consecutive_errors = 0
            if args.once:
                return 0
            if args.stop_on_found and any_found:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("已停止即時掃描。")
        return 0
    except Exception as exc:
        print(f"即時掃描失敗：{exc}", file=sys.stderr)
        return 1 if consecutive_errors >= 1 else 0
    finally:
        red_frames.destroy()


def load_map_manifest(map_dir: Path) -> dict[str, Any]:
    manifest_path = map_dir / "manifest.json"
    if not manifest_path.exists():
        return {
            "map_id": map_dir.name,
            "map_name": map_dir.name,
            "minimap": "minimap_full.png",
            "scan_points": [],
            "status": "缺 manifest.json",
        }
    with manifest_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("map_id", map_dir.name)
    data.setdefault("map_name", data["map_id"])
    data.setdefault("minimap", "minimap_full.png")
    data.setdefault("scan_points", [])
    return data


def map_image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None


def map_summaries(base_dir: Path) -> list[dict[str, Any]]:
    maps_dir = base_dir / "maps"
    summaries: list[dict[str, Any]] = []
    if not maps_dir.exists():
        return summaries

    for map_dir in sorted(path for path in maps_dir.iterdir() if path.is_dir()):
        manifest = load_map_manifest(map_dir)
        minimap_path = map_dir / str(manifest.get("minimap", "minimap_full.png"))
        scan_points = manifest.get("scan_points", [])
        if not isinstance(scan_points, list):
            scan_points = []

        missing_images = 0
        unreadable_images = 0
        for point in scan_points:
            image_value = str(point.get("image", ""))
            if not image_value:
                missing_images += 1
                continue
            image_path = map_dir / image_value
            if not image_path.exists():
                missing_images += 1
            elif map_image_size(image_path) is None:
                unreadable_images += 1

        minimap_size = map_image_size(minimap_path) if minimap_path.exists() else None
        if not minimap_path.exists():
            status = "缺小地圖"
        elif minimap_size is None:
            status = "小地圖不可讀"
        elif missing_images:
            status = f"缺 {missing_images} 張區塊圖"
        elif unreadable_images:
            status = f"{unreadable_images} 張區塊圖不可讀"
        else:
            status = "可使用"

        summaries.append({
            "map_id": str(manifest.get("map_id", map_dir.name)),
            "map_name": str(manifest.get("map_name", map_dir.name)),
            "path": str(map_dir),
            "scan_points": len(scan_points),
            "region_count": len(list((map_dir / "regions").glob("*.png"))) if (map_dir / "regions").exists() else 0,
            "minimap": str(minimap_path),
            "minimap_size": minimap_size,
            "status": status,
        })
    return summaries


def validate_maps(base_dir: Path, as_json: bool = False) -> int:
    summaries = map_summaries(base_dir)
    if as_json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    else:
        if not summaries:
            print("找不到 maps 資料夾。")
            return 1
        print("地圖檢查結果：")
        for item in summaries:
            size = item["minimap_size"]
            size_text = f"{size[0]}x{size[1]}" if size else "unknown"
            print(
                f"- {item['map_name']} ({item['map_id']}): "
                f"{item['scan_points']} 點 / regions={item['region_count']} / "
                f"minimap={size_text} / {item['status']}"
            )
    return 0 if summaries and all(item["status"] == "可使用" for item in summaries) else 1


class ScannerGui:
    def __init__(self, root: Any, config_path: Path) -> None:
        self.root = root
        self.config_path = config_path
        self.base_dir = config_path.parent
        self.windows: dict[int, dict[str, Any]] = {}
        self.window_items: dict[int, str] = {}
        self.item_to_hwnd: dict[str, int] = {}
        self.scan_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.scan_thread: threading.Thread | None = None
        self.red_frames: RedFrameManager | None = None
        self.compact_window: Any | None = None
        self.compact_status_var: Any | None = None
        self.compact_count_var: Any | None = None
        self.compact_last_var: Any | None = None
        self.last_diagnostic_saved: dict[int, float] = {}
        self.section_vars: dict[str, Any] = {}
        self.section_frames: dict[str, Any] = {}
        self.section_bodies: dict[str, Any] = {}
        self.section_body_visible: dict[str, bool] = {}
        self.section_title_vars: dict[str, Any] = {}
        self.section_base_titles: dict[str, str] = {}
        self.section_pack_options: dict[str, dict[str, Any]] = {}
        self.section_popup_menu: Any | None = None
        self.window_summary_var: Any | None = None
        self.log_var: Any | None = None
        self.running = False

        self.status_var = tk.StringVar(value="未開始")
        self.target_var = tk.StringVar(value="mysterious_examiner")
        self.interval_var = tk.StringVar(value="0.5")
        self.beep_var = tk.BooleanVar(value=True)
        self.red_frame_var = tk.BooleanVar(value=True)
        self.save_capture_var = tk.BooleanVar(value=False)
        self.diagnose_not_found_var = tk.BooleanVar(value=True)
        self.red_seconds_var = tk.StringVar(value="3")
        self.red_thickness_var = tk.StringVar(value="6")

        apply_app_icon(self.root)
        self.root.title("掃描")
        self.root.geometry("880x540")
        self.root.minsize(760, 440)
        self.root.configure(bg="#d3b260")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_ui()
        self.refresh_windows(select_flash=True)
        self.refresh_maps()

    def build_ui(self) -> None:
        if ttk is None:
            raise SystemExit("GUI 不可用：此環境沒有 tkinter.ttk。")

        self.configure_styles()
        self.build_section_menu()

        top = ttk.Frame(self.root, style="Gold.TFrame", padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Button(top, text="區塊 ▾", command=self.show_section_menu, style="Gold.TButton", width=12).pack(
            side="left", padx=(0, 10)
        )
        ttk.Label(top, text="掃描器 -", style="Gold.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.status_var, style="Gold.TLabel").pack(side="left", padx=(4, 22))
        ttk.Label(top, text="目標：神秘考官", style="Gold.TLabel").pack(side="left", padx=(0, 22))
        ttk.Button(top, text="開始掃描", command=self.start_scan, style="Gold.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(top, text="停止", command=self.stop_scan, style="Gold.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(top, text="打開結果資料夾", command=self.open_results_folder, style="Gold.TButton").pack(
            side="right"
        )

        self.content_frame = ttk.Frame(self.root, style="Gold.TFrame", padding=(8, 2, 8, 8))
        self.content_frame.pack(fill="both", expand=True)

        window_frame = self.create_collapsible_section(
            self.content_frame,
            "windows",
            "Flash 視窗清單",
            {"fill": "both", "expand": True, "pady": (0, 8)},
        )
        self.window_summary_var = tk.StringVar(value="Flash 視窗：尚未載入")
        window_controls = ttk.Frame(window_frame, style="GoldRow.TFrame", padding=(8, 6))
        window_controls.pack(fill="x", pady=(0, 6))
        ttk.Label(window_controls, textvariable=self.window_summary_var, style="GoldRow.TLabel").pack(side="left")
        ttk.Button(
            window_controls,
            text="重新整理",
            command=lambda: self.refresh_windows(select_flash=False),
            style="Gold.TButton",
        ).pack(side="left", padx=(16, 6))
        ttk.Button(window_controls, text="全選 Flash", command=self.select_flash_windows, style="Gold.TButton").pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(window_controls, text="取消全選", command=self.clear_window_selection, style="Gold.TButton").pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(window_controls, text="移除", command=self.remove_selected_windows, style="Gold.TButton").pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(window_controls, text="清空", command=self.clear_window_list, style="Gold.TButton").pack(
            side="left"
        )

        columns = ("checked", "title")
        tree_frame = ttk.Frame(window_frame, style="GoldPanel.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.window_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8, style="Gold.Treeview")
        headings = {
            "checked": "掃描",
            "title": "檔案名稱",
        }
        widths = {
            "checked": 70,
            "title": 680,
        }
        for column in columns:
            self.window_tree.heading(column, text=headings[column])
            self.window_tree.column(column, width=widths[column], anchor="w", stretch=column == "title")
        self.window_tree.tag_configure("found", foreground="#c62828")
        self.window_tree.bind("<Button-1>", self.on_window_click)
        self.window_tree.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.window_tree.yview)
        yscroll.pack(side="right", fill="y")
        self.window_tree.configure(yscrollcommand=yscroll.set)

        settings = self.create_collapsible_section(
            self.content_frame,
            "settings",
            "掃描設定",
            {"fill": "x", "pady": (0, 8)},
        )
        settings_row = ttk.Frame(settings, style="GoldRow.TFrame", padding=(8, 6))
        settings_row.pack(fill="x")
        ttk.Checkbutton(settings_row, text="提示音", variable=self.beep_var, style="Gold.TCheckbutton").pack(
            side="left", padx=(0, 12)
        )
        ttk.Checkbutton(settings_row, text="紅框", variable=self.red_frame_var, style="Gold.TCheckbutton").pack(
            side="left", padx=(0, 12)
        )
        ttk.Checkbutton(
            settings_row,
            text="未找到時保存診斷",
            variable=self.diagnose_not_found_var,
            style="Gold.TCheckbutton",
        ).pack(side="left", padx=(0, 18))
        ttk.Label(settings_row, text="間隔秒數：", style="GoldRow.TLabel").pack(side="left")
        ttk.Entry(settings_row, textvariable=self.interval_var, width=8, style="Gold.TEntry").pack(
            side="left", padx=(4, 18)
        )
        ttk.Label(settings_row, text=f"診斷保存：最近 {DIAGNOSTIC_MAX_RECORDS} 筆 / 每窗 10 秒", style="GoldRow.TLabel").pack(
            side="left", padx=(0, 18)
        )
        ttk.Checkbutton(
            settings_row,
            text="保留截圖（覆蓋，不累積）",
            variable=self.save_capture_var,
            style="Gold.TCheckbutton",
        ).pack(side="left")

        advanced_row = ttk.Frame(settings, style="GoldRow.TFrame", padding=(8, 0, 8, 6))
        advanced_row.pack(fill="x")
        ttk.Label(advanced_row, text="紅框秒數：", style="GoldRow.TLabel").pack(side="left")
        ttk.Entry(advanced_row, textvariable=self.red_seconds_var, width=8, style="Gold.TEntry").pack(
            side="left", padx=(4, 18)
        )
        ttk.Label(advanced_row, text="紅框粗細：", style="GoldRow.TLabel").pack(side="left")
        ttk.Entry(advanced_row, textvariable=self.red_thickness_var, width=8, style="Gold.TEntry").pack(
            side="left", padx=(4, 18)
        )
        ttk.Label(advanced_row, text="勾選數量不限制；Flash 視窗不可互相遮蓋。", style="GoldRow.TLabel").pack(
            side="left"
        )

        maps = self.create_collapsible_section(
            self.content_frame,
            "maps",
            "地圖資料",
            {"fill": "both", "expand": False, "pady": (0, 8)},
            collapsed=True,
        )
        map_columns = ("name", "id", "points", "regions", "minimap", "status")
        self.map_tree = ttk.Treeview(maps, columns=map_columns, show="headings", height=5, style="Gold.Treeview")
        for column, title, width in [
            ("name", "地圖", 120),
            ("id", "資料夾", 220),
            ("points", "點數", 60),
            ("regions", "圖片", 60),
            ("minimap", "小地圖", 90),
            ("status", "狀態", 100),
        ]:
            self.map_tree.heading(column, text=title)
            self.map_tree.column(column, width=width, anchor="w", stretch=column in {"id", "status"})
        self.map_tree.pack(fill="both", expand=True)
        map_buttons = ttk.Frame(maps, style="GoldPanel.TFrame")
        map_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(map_buttons, text="重新載入地圖", command=self.refresh_maps, style="Gold.TButton").pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(map_buttons, text="打開資料夾", command=self.open_maps_folder, style="Gold.TButton").pack(
            side="left"
        )

        log_frame = self.create_collapsible_section(
            self.content_frame,
            "log",
            "狀態紀錄",
            {"fill": "x"},
            collapsed=True,
        )
        self.log_var = tk.StringVar(value="控制台已開啟。")
        ttk.Label(log_frame, textvariable=self.log_var, style="GoldPanel.TLabel", wraplength=820).pack(
            fill="x", padx=4, pady=2
        )

    def configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        base_bg = "#d3b260"
        row_bg = "#cba95a"
        panel_bg = "#dabc6f"
        header_bg = "#c79f49"
        button_bg = "#efcf85"
        button_active = "#f4d99a"
        entry_bg = "#fff0bf"
        text_color = "#151107"
        line_color = "#79581b"
        self.root.option_add("*Font", ("Microsoft JhengHei UI", 9))
        style.configure(".", background=base_bg, foreground=text_color)
        style.configure("Gold.TFrame", background=base_bg)
        style.configure("GoldPanel.TFrame", background=panel_bg)
        style.configure("GoldRow.TFrame", background=row_bg)
        style.configure("Gold.TLabel", background=base_bg, foreground=text_color)
        style.configure("GoldPanel.TLabel", background=panel_bg, foreground=text_color)
        style.configure("GoldRow.TLabel", background=row_bg, foreground=text_color)
        style.configure("Gold.TButton", background=button_bg, foreground=text_color, bordercolor=line_color, padding=(8, 3))
        style.map("Gold.TButton", background=[("pressed", header_bg), ("active", button_active)])
        style.configure("GoldHeader.TButton", background=header_bg, foreground=text_color, anchor="w", padding=(8, 4))
        style.map("GoldHeader.TButton", background=[("pressed", "#b68e35"), ("active", "#d0aa52")])
        style.configure("Gold.TCheckbutton", background=row_bg, foreground=text_color)
        style.map("Gold.TCheckbutton", background=[("active", row_bg)])
        style.configure("Gold.TEntry", fieldbackground=entry_bg, foreground=text_color)
        style.configure(
            "Gold.Treeview",
            background=entry_bg,
            fieldbackground=entry_bg,
            foreground=text_color,
            bordercolor=line_color,
            rowheight=25,
        )
        style.configure("Gold.Treeview.Heading", background="#d4b56b", foreground=text_color, relief="flat")
        style.map("Gold.Treeview", background=[("selected", "#c79f49")], foreground=[("selected", text_color)])
        style.configure("Found.Treeview", foreground="#c62828")

    def build_section_menu(self) -> None:
        menu_bar = tk.Menu(self.root)
        section_menu = tk.Menu(menu_bar, tearoff=0)
        self.section_popup_menu = section_menu
        self.section_vars = {
            "windows": tk.BooleanVar(value=True),
            "settings": tk.BooleanVar(value=True),
            "maps": tk.BooleanVar(value=True),
            "log": tk.BooleanVar(value=True),
        }
        labels = {
            "windows": "掃描視窗",
            "settings": "掃描設定",
            "maps": "地圖資料",
            "log": "狀態紀錄",
        }
        for key in ("windows", "settings", "maps", "log"):
            section_menu.add_checkbutton(
                label=labels[key],
                variable=self.section_vars[key],
                command=self.refresh_sections,
            )
        section_menu.add_separator()
        section_menu.add_command(label="全部展開", command=lambda: self.set_all_sections(True))
        section_menu.add_command(label="全部收起", command=lambda: self.set_all_sections(False))
        menu_bar.add_cascade(label="區塊", menu=section_menu)
        self.root.config(menu=menu_bar)

    def show_section_menu(self) -> None:
        if self.section_popup_menu is None:
            return
        try:
            self.section_popup_menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            self.section_popup_menu.grab_release()

    def create_collapsible_section(
        self,
        parent: Any,
        key: str,
        title: str,
        pack_options: dict[str, Any],
        collapsed: bool = False,
    ) -> Any:
        wrapper = ttk.Frame(parent, style="Gold.TFrame")
        wrapper.pack(**pack_options)
        header_var = tk.StringVar()
        header_button = ttk.Button(
            wrapper,
            textvariable=header_var,
            command=lambda section_key=key: self.toggle_section_body(section_key),
            style="GoldHeader.TButton",
        )
        header_button.pack(fill="x")
        body = ttk.Frame(wrapper, style="GoldPanel.TFrame", padding=8)

        self.section_frames[key] = wrapper
        self.section_bodies[key] = body
        self.section_body_visible[key] = not collapsed
        self.section_title_vars[key] = header_var
        self.section_base_titles[key] = title
        self.section_pack_options[key] = pack_options
        self.refresh_section_body(key)
        return body

    def refresh_section_body(self, key: str) -> None:
        body = self.section_bodies.get(key)
        title_var = self.section_title_vars.get(key)
        if body is None or title_var is None:
            return
        visible = self.section_body_visible.get(key, True)
        if visible:
            body.pack(fill="both", expand=key in {"windows", "maps"})
        else:
            body.pack_forget()
        arrow = "▼" if visible else "▶"
        title = self.section_base_titles.get(key, key)
        if key == "windows" and self.window_summary_var is not None:
            title = f"{title}（{self.selected_window_count()} 個已勾選）"
        title_var.set(f"{arrow} {title}")

    def toggle_section_body(self, key: str) -> None:
        self.section_body_visible[key] = not self.section_body_visible.get(key, True)
        self.refresh_section_body(key)

    def refresh_sections(self) -> None:
        for frame in self.section_frames.values():
            frame.pack_forget()
        for key in ("windows", "settings", "maps", "log"):
            if self.section_vars[key].get():
                self.section_frames[key].pack(**self.section_pack_options[key])

    def set_all_sections(self, visible: bool) -> None:
        for key, var in self.section_vars.items():
            var.set(True)
            self.section_body_visible[key] = visible
            self.refresh_section_body(key)
        self.refresh_sections()

    def flash_like(self, title: str) -> bool:
        return "Adobe Flash Player" in title or "Flash" in title

    def display_window_name(self, title: str) -> str:
        return title.strip() or "Flash 視窗"

    def selected_window_count(self) -> int:
        return sum(1 for row in self.windows.values() if row["selected"])

    def update_window_summary(self) -> None:
        selected_count = self.selected_window_count()
        if self.window_summary_var is not None:
            self.window_summary_var.set(f"Flash 視窗：已載入 {len(self.windows)} 個 / 已勾選 {selected_count} 個")
        self.refresh_section_body("windows")

    def window_size_text(self, hwnd: int) -> str:
        try:
            left, top, right, bottom = client_bbox(hwnd)
            return f"{right - left}x{bottom - top}  {left:+d},{top:+d}"
        except Exception:
            return "unknown"

    def refresh_windows(self, select_flash: bool = False) -> None:
        try:
            enable_dpi_awareness()
            all_windows = visible_windows()
        except Exception as exc:
            self.set_status(f"列視窗失敗：{exc}")
            return

        previous_selection = {hwnd: row["selected"] for hwnd, row in self.windows.items()}
        self.windows.clear()
        self.window_items.clear()
        self.item_to_hwnd.clear()
        for item in self.window_tree.get_children():
            self.window_tree.delete(item)

        flash_windows = [(hwnd, title) for hwnd, title in all_windows if self.flash_like(title)]
        for index, (hwnd, title) in enumerate(flash_windows, start=1):
            selected = True if select_flash else bool(previous_selection.get(hwnd, False))
            row = {
                "index": index,
                "title": title,
                "display_name": self.display_window_name(title),
                "selected": selected,
                "result": "未掃描" if selected else "未勾選",
                "score": "--",
            }
            self.windows[hwnd] = row
            item_id = self.window_tree.insert(
                "",
                "end",
                values=(
                    "[x]" if selected else "[ ]",
                    row["display_name"],
                ),
            )
            self.window_items[hwnd] = item_id
            self.item_to_hwnd[item_id] = hwnd
        self.update_window_summary()
        self.set_status(f"已載入 {len(self.windows)} 個 Flash 視窗")
        self.log(f"已載入 {len(self.windows)} 個 Flash 視窗。")

    def refresh_maps(self) -> None:
        for item in self.map_tree.get_children():
            self.map_tree.delete(item)
        summaries = map_summaries(self.base_dir)
        for item in summaries:
            size = item["minimap_size"]
            size_text = f"{size[0]}x{size[1]}" if size else "缺"
            self.map_tree.insert(
                "",
                "end",
                values=(
                    item["map_name"],
                    item["map_id"],
                    item["scan_points"],
                    item["region_count"],
                    size_text,
                    item["status"],
                ),
            )
        ok_count = sum(1 for item in summaries if item["status"] == "可使用")
        self.log(f"地圖檢查：{ok_count}/{len(summaries)} 可使用。")

    def on_window_click(self, event: Any) -> None:
        if self.running:
            return
        region = self.window_tree.identify("region", event.x, event.y)
        column = self.window_tree.identify_column(event.x)
        row_id = self.window_tree.identify_row(event.y)
        if region != "cell" or column != "#1" or not row_id:
            return
        values = self.window_tree.item(row_id, "values")
        if not values:
            return
        hwnd = self.item_to_hwnd.get(row_id)
        if hwnd is None:
            return
        self.windows[hwnd]["selected"] = not self.windows[hwnd]["selected"]
        self.update_window_row(hwnd)

    def update_window_row(self, hwnd: int, result: str | None = None, score: str | None = None, found: bool = False) -> None:
        row = self.windows.get(hwnd)
        item_id = self.window_items.get(hwnd)
        if row is None or item_id is None:
            return
        if result is not None:
            row["result"] = result
        if score is not None:
            row["score"] = score
        self.window_tree.item(
            item_id,
            values=(
                "[x]" if row["selected"] else "[ ]",
                row["display_name"],
            ),
            tags=("found",) if found else (),
        )
        self.update_window_summary()

    def selected_windows(self) -> list[tuple[int, str]]:
        return [(hwnd, row["title"]) for hwnd, row in self.windows.items() if row["selected"]]

    def select_flash_windows(self) -> None:
        if self.running:
            return
        for hwnd, row in self.windows.items():
            row["selected"] = True
            row["result"] = "未掃描" if row["selected"] else "未勾選"
            row["score"] = "--"
            self.update_window_row(hwnd)
        self.log_selected_windows()

    def clear_window_selection(self) -> None:
        if self.running:
            return
        for hwnd, row in self.windows.items():
            row["selected"] = False
            row["result"] = "未勾選"
            row["score"] = "--"
            self.update_window_row(hwnd)
        self.log("已取消所有視窗勾選。")

    def remove_selected_windows(self) -> None:
        if self.running:
            return
        removed = 0
        for hwnd, row in list(self.windows.items()):
            if not row["selected"]:
                continue
            item_id = self.window_items.pop(hwnd, None)
            if item_id is not None:
                self.item_to_hwnd.pop(item_id, None)
                self.window_tree.delete(item_id)
            self.windows.pop(hwnd, None)
            removed += 1
        self.update_window_summary()
        self.log(f"已從清單移除 {removed} 個勾選視窗。")

    def clear_window_list(self) -> None:
        if self.running:
            return
        for item in self.window_tree.get_children():
            self.window_tree.delete(item)
        self.windows.clear()
        self.window_items.clear()
        self.item_to_hwnd.clear()
        self.update_window_summary()
        self.log("已清空 Flash 視窗清單。")

    def log_selected_windows(self) -> None:
        handles = [str(hwnd) for hwnd, _title in self.selected_windows()]
        self.log(f"目前勾選 {len(handles)} 個視窗：{','.join(handles) if handles else '無'}")

    def parse_float(self, value: str, default: float, minimum: float) -> float:
        try:
            return max(minimum, float(value))
        except Exception:
            return default

    def parse_int(self, value: str, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(value))
        except Exception:
            return default

    def start_scan(self) -> None:
        if self.running:
            return
        windows = self.selected_windows()
        if not windows:
            self.alert("請先勾選至少一個 Flash 視窗。")
            return
        try:
            scanner = PreparedScanner(self.config_path, self.target_var.get().strip() or None)
        except Exception as exc:
            self.alert(f"建立掃描器失敗：{exc}")
            return

        for hwnd, row in self.windows.items():
            if row["selected"]:
                row["result"] = "掃描中"
                row["score"] = "--"
                self.update_window_row(hwnd)

        interval = self.parse_float(self.interval_var.get(), 0.5, 0.1)
        red_seconds = self.parse_float(self.red_seconds_var.get(), 3.0, 0.5)
        red_thickness = self.parse_int(self.red_thickness_var.get(), 6, 2)
        capture_dir = self.base_dir / "results"
        diagnostic_dir = capture_dir / "diagnostics"
        save_capture = bool(self.save_capture_var.get())
        diagnose_not_found = bool(self.diagnose_not_found_var.get())
        if save_capture or diagnose_not_found:
            capture_dir.mkdir(parents=True, exist_ok=True)
        if diagnose_not_found:
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
        self.last_diagnostic_saved.clear()

        self.stop_event = threading.Event()
        self.red_frames = RedFrameManager(
            enabled=bool(self.red_frame_var.get()),
            thickness=red_thickness,
            seconds=red_seconds,
            root=self.root,
        )
        self.running = True
        self.set_status("掃描中")
        self.log(f"開始掃描 {len(windows)} 個視窗。")
        self.show_compact_window(len(windows))
        self.root.withdraw()
        self.scan_thread = threading.Thread(
            target=self.scan_worker,
            args=(windows, scanner, interval, save_capture, capture_dir, diagnose_not_found, diagnostic_dir, self.stop_event),
            daemon=True,
        )
        self.scan_thread.start()
        self.root.after(100, self.process_scan_queue)

    def stop_scan(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        if self.running:
            self.set_status("停止中")
            self.log("正在停止掃描。")

    def scan_worker(
        self,
        windows: list[tuple[int, str]],
        scanner: PreparedScanner,
        interval: float,
        save_capture: bool,
        capture_dir: Path,
        diagnose_not_found: bool,
        diagnostic_dir: Path,
        stop_event: threading.Event,
    ) -> None:
        try:
            while not stop_event.is_set():
                for index, (hwnd, title) in enumerate(windows, start=1):
                    if stop_event.is_set():
                        break
                    try:
                        bbox = client_bbox(hwnd)
                        image = ImageGrab.grab(bbox=bbox).convert("RGB")
                        if save_capture:
                            image.save(capture_dir / f"gui_capture_{hwnd}.png")
                        result = scanner.scan_pil(image, f"hwnd={hwnd}")
                        if diagnose_not_found and not result.get("found"):
                            now = time.time()
                            last_saved = self.last_diagnostic_saved.get(hwnd, 0.0)
                            result["diagnostic_hint"] = diagnostic_hint(result)
                            if now - last_saved >= DIAGNOSTIC_SAVE_INTERVAL_SECONDS:
                                save_not_found_diagnostic(image, result, diagnostic_dir, hwnd, title, bbox)
                                self.last_diagnostic_saved[hwnd] = now
                        self.scan_queue.put(("result", hwnd, index, len(windows), bbox, result))
                    except Exception as exc:
                        self.scan_queue.put(("error", hwnd, str(exc)))
                stop_event.wait(interval)
        finally:
            self.scan_queue.put(("done",))

    def process_scan_queue(self) -> None:
        while True:
            try:
                item = self.scan_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "result":
                _kind, hwnd, index, total, bbox, result = item
                self.handle_scan_result(int(hwnd), int(index), int(total), bbox, result)
            elif kind == "error":
                _kind, hwnd, message = item
                self.update_window_row(int(hwnd), "掃描失敗", "--")
                self.log(f"hwnd={hwnd} 掃描失敗：{message}")
            elif kind == "done":
                self.running = False
                self.set_status("已停止")
                if self.red_frames is not None:
                    self.red_frames.destroy()
                    self.red_frames = None
                self.log("掃描已停止。")
                self.hide_compact_window()
                self.root.deiconify()
                self.root.lift()

        if self.red_frames is not None:
            self.red_frames.cleanup()
        if self.running or not self.scan_queue.empty():
            self.root.after(100, self.process_scan_queue)

    def handle_scan_result(
        self,
        hwnd: int,
        index: int,
        total: int,
        bbox: tuple[int, int, int, int],
        result: dict[str, Any],
    ) -> None:
        best = result.get("best")
        found = bool(result.get("found") and best)
        if found:
            score = str(best.get("score", "--"))
            self.update_window_row(hwnd, "找到神秘考官", score, found=True)
            self.update_compact_status(f"找到神秘考官：{self.windows.get(hwnd, {}).get('display_name', hwnd)}")
            screen_x = bbox[0] + int(best["center_x"])
            screen_y = bbox[1] + int(best["center_y"])
            self.log(
                f"[{index}/{total}] hwnd={hwnd} 找到：神秘考官，"
                f"畫面X={best['center_x']} 畫面Y={best['center_y']} "
                f"螢幕X={screen_x} 螢幕Y={screen_y} 分數={score}"
            )
            if self.red_frames is not None:
                self.red_frames.show(hwnd, bbox)
            if self.beep_var.get():
                try:
                    import winsound

                    winsound.Beep(1200, 180)
                except Exception:
                    pass
        else:
            score = str(best.get("score", "--")) if best else "--"
            self.update_window_row(hwnd, "未找到", score)
            if best:
                threshold = best.get("threshold", "--")
                template = best.get("template", "--")
                hint = result.get("diagnostic_hint") or diagnostic_hint(result)
                summary = f"未找到，最高分數 {score}/{threshold}，最高模板 {template}"
            else:
                hint = result.get("diagnostic_hint") or diagnostic_hint(result)
                summary = "未找到，沒有有效模板比對結果"

            diagnostic_image = result.get("diagnostic_image")
            if diagnostic_image:
                self.log(f"[{index}/{total}] hwnd={hwnd} {summary}；診斷圖：{diagnostic_image}；提示：{hint}")
                self.update_compact_status(f"未找到：最高 {score}，診斷已保存")
            else:
                self.update_compact_status(f"掃描中：{index}/{total}，{summary}")

    def show_compact_window(self, selected_count: int) -> None:
        if self.compact_window is None or not self.compact_window.winfo_exists():
            self.compact_window = tk.Toplevel(self.root)
            apply_app_icon(self.compact_window)
            self.compact_window.title("掃描")
            self.compact_window.geometry("360x132+80+80")
            self.compact_window.resizable(False, False)
            self.compact_window.attributes("-topmost", True)
            self.compact_window.configure(bg="#d3b260")
            self.compact_window.protocol("WM_DELETE_WINDOW", self.restore_main_window)
            frame = ttk.Frame(self.compact_window, style="Gold.TFrame", padding=12)
            frame.pack(fill="both", expand=True)
            self.compact_status_var = tk.StringVar(value="掃描中")
            self.compact_count_var = tk.StringVar(value=f"Flash 視窗：{selected_count}")
            self.compact_last_var = tk.StringVar(value="等待第一次掃描結果。")
            ttk.Label(
                frame,
                textvariable=self.compact_status_var,
                font=("Microsoft JhengHei UI", 10, "bold"),
                style="Gold.TLabel",
            ).pack(anchor="w")
            ttk.Label(frame, textvariable=self.compact_count_var, style="Gold.TLabel").pack(anchor="w", pady=(4, 0))
            ttk.Label(frame, textvariable=self.compact_last_var, style="Gold.TLabel").pack(anchor="w", pady=(2, 8))
            buttons = ttk.Frame(frame, style="Gold.TFrame")
            buttons.pack(fill="x")
            ttk.Button(buttons, text="展開", command=self.restore_main_window, style="Gold.TButton").pack(side="left")
            ttk.Button(buttons, text="停止", command=self.stop_scan, style="Gold.TButton").pack(side="right")
        else:
            self.compact_window.deiconify()
            self.compact_count_var.set(f"Flash 視窗：{selected_count}")
        self.update_compact_status("掃描中")

    def hide_compact_window(self) -> None:
        if self.compact_window is not None and self.compact_window.winfo_exists():
            self.compact_window.withdraw()

    def restore_main_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.hide_compact_window()

    def update_compact_status(self, value: str) -> None:
        if self.compact_window is None or not self.compact_window.winfo_exists():
            return
        if self.compact_status_var is not None:
            self.compact_status_var.set(self.status_var.get())
        if self.compact_last_var is not None:
            self.compact_last_var.set(value)

    def open_maps_folder(self) -> None:
        maps_dir = self.base_dir / "maps"
        try:
            os.startfile(str(maps_dir))
        except Exception as exc:
            self.alert(f"無法打開資料夾：{exc}")

    def open_results_folder(self) -> None:
        results_dir = self.base_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(results_dir))
        except Exception as exc:
            self.alert(f"無法打開資料夾：{exc}")

    def set_status(self, value: str) -> None:
        self.status_var.set(value)

    def log(self, value: str) -> None:
        if self.log_var is not None:
            self.log_var.set(value)

    def alert(self, value: str) -> None:
        self.log(value)
        if messagebox is not None:
            messagebox.showwarning("掃描", value)

    def close(self) -> None:
        self.stop_scan()
        if self.red_frames is not None:
            self.red_frames.destroy()
        if self.compact_window is not None and self.compact_window.winfo_exists():
            self.compact_window.destroy()
        self.root.destroy()


def run_gui(config_path: Path) -> int:
    if tk is None or ttk is None:
        print("GUI 不可用：此環境沒有 tkinter。", file=sys.stderr)
        return 1
    set_app_user_model_id()
    root = tk.Tk()
    ScannerGui(root, config_path)
    root.mainloop()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="單畫面模板掃描器")
    parser.add_argument("--image", help="要掃描的完整遊戲畫面 PNG/JPG")
    parser.add_argument("--target", help="指定目標 id，例如 road_t")
    parser.add_argument("--config", default="config.json", help="設定檔路徑")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    parser.add_argument("--no-debug", action="store_true", help="不要輸出 debug 標框圖")
    parser.add_argument("--self-test", action="store_true", help="掃描 samples 資料夾內的測試圖片")
    parser.add_argument("--gui", action="store_true", help="開啟掃描器控制台")
    parser.add_argument("--validate-maps", action="store_true", help="檢查 maps 資料夾內所有地圖資料")
    parser.add_argument("--list-windows", action="store_true", help="列出目前可掃描的視窗標題")
    parser.add_argument("--watch", action="store_true", help="即時擷取遊戲視窗並持續掃描")
    parser.add_argument("--window-handle", help="指定 --list-windows 顯示的視窗編號")
    parser.add_argument("--window-handles", help="指定多個視窗編號，用逗號分隔，例如 123,456,789")
    parser.add_argument("--multi-watch", action="store_true", help="多視窗模式；搭配 --window-handles 或 --window-title 使用")
    parser.add_argument("--window-title", help="指定要掃描的視窗標題關鍵字")
    parser.add_argument("--bring-to-front", action="store_true", help="開始掃描前將指定視窗拉到前景")
    parser.add_argument("--focus-delay", type=float, default=3.0, help="未指定視窗標題時，等待幾秒讓你切到遊戲視窗")
    parser.add_argument("--interval", type=float, default=1.0, help="即時掃描間隔秒數")
    parser.add_argument("--once", action="store_true", help="即時模式只掃描一次就結束")
    parser.add_argument("--stop-on-found", action="store_true", help="找到目標後立即結束掃描")
    parser.add_argument("--live-capture", default="results/live_capture.png", help="即時擷取暫存圖片路徑")
    parser.add_argument("--save-live-capture", action="store_true", help="即時模式保留每次擷取的畫面，方便診斷")
    parser.add_argument("--show-not-found", action="store_true", help="即時模式下每次未找到也輸出一行")
    parser.add_argument("--beep", action="store_true", help="即時模式找到目標時發出提示音")
    parser.add_argument("--no-red-frame", action="store_true", help="找到目標時不要顯示紅框")
    parser.add_argument("--red-frame-seconds", type=float, default=3.0, help="找到時紅框保留秒數")
    parser.add_argument("--red-frame-thickness", type=int, default=6, help="找到時紅框粗細")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if getattr(sys, "frozen", False):
        script_dir = Path(sys.executable).resolve().parent
    else:
        script_dir = Path(__file__).resolve().parent
    config_path = resolve_path(script_dir, args.config)
    save_debug = not args.no_debug

    if args.gui:
        return run_gui(config_path)

    if args.validate_maps:
        return validate_maps(config_path.parent, as_json=args.json)

    if args.list_windows:
        print_window_list()
        return 0

    if args.self_test:
        self_test(config_path, save_debug)
        return 0

    if args.watch:
        return watch_window(config_path, args)

    if len(sys.argv) == 1:
        return run_gui(config_path)

    if not args.image:
        print("請指定 --image，或使用 --gui / --self-test / --watch / --list-windows。", file=sys.stderr)
        return 2

    image_path = resolve_path(Path.cwd(), args.image)
    result = scan_image(config_path, image_path, args.target, save_debug)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
