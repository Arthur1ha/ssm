"""Cheap camera-change admission against the last successfully analyzed frame.

This is a synchronous image adapter, not a semantic observer. Its decision only
indicates when another visual observation may be useful; it proves no object,
identity, posture, or permission. Callers own sampling, deadlines and epochs.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from threading import Lock
from typing import Hashable

import cv2
import numpy as np


@dataclass(frozen=True)
class SceneFrameToken:
    """The exact reduced image offered for analysis, bound to one gate epoch."""

    sequence: int
    frame_id: Hashable | None
    _sample: np.ndarray = field(repr=False, compare=False)
    _owner: object = field(repr=False, compare=False)
    _generation: int = field(repr=False, compare=False)


@dataclass(frozen=True)
class SceneChangeObservation:
    changed: bool
    initial: bool
    token: SceneFrameToken
    changed_fraction: float
    local_fraction: float


class SceneChangeGate:
    """Confirm substantial global or local differences on consecutive samples.

    ``observe`` never advances the reference image. ``mark_analyzed`` commits
    only its successful analysis token, even if newer frames were sampled in
    the meantime. The next comparison therefore retains slow scene changes.
    Thresholds concern downsampled, blurred luminance and need camera tuning.
    """

    def __init__(self, *, sample_size: tuple[int, int] = (160, 120),
                 pixel_threshold: float = 20, global_fraction: float = .05,
                 local_fraction: float = .25, block_size: int = 16,
                 confirmations: int = 2, blur_size: int = 5,
                 suppress_brightness: bool = True) -> None:
        if (not isinstance(sample_size, tuple) or len(sample_size) != 2
                or any(type(size) is not int or size < 1 for size in sample_size)):
            raise ValueError('invalid_scene_sample_size')
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (pixel_threshold, global_fraction, local_fraction)):
            raise ValueError('invalid_scene_change_threshold')
        if not 0 < pixel_threshold <= 255 or not 0 < global_fraction <= 1 or not 0 < local_fraction <= 1:
            raise ValueError('invalid_scene_change_threshold')
        if (type(block_size) is not int or block_size < 1
                or type(confirmations) is not int or confirmations < 1
                or type(blur_size) is not int or blur_size < 1 or blur_size % 2 != 1
                or type(suppress_brightness) is not bool):
            raise ValueError('invalid_scene_change_configuration')
        self._size = sample_size
        self._pixel_threshold = pixel_threshold
        self._global_threshold = global_fraction
        self._local_threshold = local_fraction
        self._block_size = block_size
        self._confirmations = confirmations
        self._blur_size = blur_size
        self._suppress_brightness = suppress_brightness
        self._owner = object()
        self._lock = Lock()
        self._generation = 0
        self._sequence = 0
        self._baseline: SceneFrameToken | None = None
        self._recent: deque[SceneFrameToken] = deque(maxlen=confirmations)
        self._consecutive = 0

    def reset(self) -> None:
        """Retire both reference and outstanding tokens for a camera/demand epoch."""
        with self._lock:
            self._generation += 1
            self._baseline = None
            self._recent.clear()
            self._consecutive = 0

    def _prepare(self, jpeg: bytes) -> np.ndarray:
        if not isinstance(jpeg, bytes) or not jpeg:
            raise ValueError('invalid_scene_image')
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError('invalid_scene_image')
        image = cv2.resize(image, self._size, interpolation=cv2.INTER_AREA)
        image = cv2.GaussianBlur(image, (self._blur_size, self._blur_size), 0)
        image.setflags(write=False)
        return image

    def _difference(self, token: SceneFrameToken) -> tuple[bool, float, float]:
        if self._baseline is None:
            return True, 1.0, 1.0
        difference = token._sample.astype(np.float32) - self._baseline._sample.astype(np.float32)
        if self._suppress_brightness:
            difference -= np.median(difference)
        mask = (np.abs(difference) > self._pixel_threshold).astype(np.float32)
        fraction = float(mask.mean())
        # Overlapping windows avoid missing a small target on a tile boundary.
        local = float(cv2.boxFilter(mask, -1, (self._block_size, self._block_size),
                                    normalize=True, borderType=cv2.BORDER_CONSTANT).max())
        return fraction >= self._global_threshold or local >= self._local_threshold, fraction, local

    def observe(self, jpeg: bytes, *, frame_id: Hashable | None = None) -> SceneChangeObservation:
        """Inspect one sample; duplicate provided identities cannot confirm twice.

        Local sequence numbers record admission order only. Camera frame IDs
        need not be numeric or monotonic; the caller resets on epoch changes.
        """
        with self._lock:
            generation = self._generation
        sample = self._prepare(jpeg)
        with self._lock:
            if generation != self._generation:
                # Decoding may finish after reset; do not seed the new epoch.
                token = SceneFrameToken(self._sequence, frame_id, sample, self._owner, generation)
                return SceneChangeObservation(False, False, token, 0.0, 0.0)
            duplicate = bool(frame_id is not None and self._recent
                             and self._recent[-1].frame_id == frame_id)
            if duplicate:
                token = self._recent[-1]
            else:
                self._sequence += 1
                token = SceneFrameToken(self._sequence, frame_id, sample, self._owner, generation)
                self._recent.append(token)
            significant, fraction, local = self._difference(token)
            initial = self._baseline is None
            if not duplicate:
                self._consecutive = self._consecutive + 1 if significant else 0
            return SceneChangeObservation(initial or significant and self._consecutive >= self._confirmations,
                                          initial, token, fraction, local)

    def mark_analyzed(self, token: SceneFrameToken) -> bool:
        """Commit that exact successful frame; late/foreign/retired acks lose."""
        with self._lock:
            if (not isinstance(token, SceneFrameToken) or token._owner is not self._owner
                    or token._generation != self._generation
                    or self._baseline is not None and token.sequence <= self._baseline.sequence):
                return False
            self._baseline = token
            self._consecutive = 0
            # Preserve confirmations of changes that arrived during inference.
            for observed in self._recent:
                if observed.sequence > token.sequence:
                    significant, _fraction, _local = self._difference(observed)
                    self._consecutive = self._consecutive + 1 if significant else 0
            return True
