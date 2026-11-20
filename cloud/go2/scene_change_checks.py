"""Synthetic JPEG checks for local change admission; no model/device/storage."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import cv2
import numpy as np
import pytest

from cloud.go2.perception.scene_change import SceneChangeGate


def jpeg(image):
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def scene():
    image = np.full((240, 320), 100, dtype=np.uint8)
    cv2.rectangle(image, (25, 30), (100, 200), 125, -1)
    cv2.rectangle(image, (220, 20), (290, 170), 145, -1)
    return image


def with_target(image, *, x=145, y=100, size=24):
    image = image.copy()
    cv2.rectangle(image, (x, y), (x + size, y + size), 235, -1)
    return image


def initialized(**kwargs):
    gate = SceneChangeGate(**kwargs)
    first = gate.observe(jpeg(scene()), frame_id='first')
    assert first.initial and first.changed
    assert gate.mark_analyzed(first.token)
    return gate, first


def test_initial_ack_and_static_scene_do_not_repeatedly_admit_analysis():
    gate, _first = initialized()
    for index in range(5):
        observed = gate.observe(jpeg(scene()), frame_id=index)
        assert not observed.changed and not observed.initial
        assert observed.changed_fraction == 0


def test_global_change_requires_two_consecutive_samples_and_same_frame_cannot_confirm_twice():
    gate, _first = initialized()
    moved = scene()
    cv2.rectangle(moved, (90, 40), (210, 200), 230, -1)
    first = gate.observe(jpeg(moved), frame_id='changed-1')
    assert first.changed_fraction > .05 and not first.changed
    duplicate = gate.observe(jpeg(moved), frame_id='changed-1')
    assert duplicate.token is first.token and not duplicate.changed
    confirmed = gate.observe(jpeg(moved), frame_id='changed-2')
    assert confirmed.changed
    assert gate.mark_analyzed(confirmed.token)
    assert not gate.observe(jpeg(moved), frame_id='changed-3').changed


def test_a_small_local_target_below_global_threshold_is_detected_across_tile_boundaries():
    gate, _first = initialized()
    moved = with_target(scene(), x=151, y=111)
    first = gate.observe(jpeg(moved), frame_id=1)
    assert first.changed_fraction < .05
    assert first.local_fraction >= .25 and not first.changed
    assert gate.observe(jpeg(moved), frame_id=2).changed


def test_brightness_offset_noise_and_mild_camera_blur_are_suppressed():
    gate, _first = initialized()
    original = scene()
    random = np.random.default_rng(41)
    variants = [original.astype(np.int16) + 35,
                original.astype(np.int16) - 25,
                original.astype(np.int16) + random.normal(0, 8, original.shape),
                cv2.GaussianBlur(original, (9, 9), 0)]
    for index, variant in enumerate(variants * 2):
        variant = np.clip(variant, 0, 255).astype(np.uint8)
        assert not gate.observe(jpeg(variant), frame_id=index).changed


def test_slow_accumulation_is_measured_against_analyzed_frame_not_adjacent_samples():
    gate, _first = initialized()
    original = scene()
    for amount in (5, 10, 15):
        changed = original.copy()
        changed[60:180, 110:210] += amount
        assert not gate.observe(jpeg(changed), frame_id=amount).changed
    changed[60:180, 110:210] = original[60:180, 110:210] + 25
    assert not gate.observe(jpeg(changed), frame_id=25).changed
    changed[60:180, 110:210] = original[60:180, 110:210] + 30
    assert gate.observe(jpeg(changed), frame_id=30).changed


def test_isolated_flash_does_not_confirm_and_static_return_resets_confirmation():
    gate, _first = initialized()
    target = jpeg(with_target(scene()))
    assert not gate.observe(target, frame_id=1).changed
    assert not gate.observe(jpeg(scene()), frame_id=2).changed
    assert not gate.observe(target, frame_id=3).changed
    assert gate.observe(target, frame_id=4).changed


def test_success_ack_uses_its_original_frame_and_preserves_changes_during_analysis():
    gate, _first = initialized()
    target_a = jpeg(with_target(scene(), x=130))
    target_b = jpeg(with_target(scene(), x=185))
    gate.observe(target_a, frame_id=1)
    analyzed_a = gate.observe(target_a, frame_id=2)
    assert analyzed_a.changed
    gate.observe(target_b, frame_id=3)
    latest_b = gate.observe(target_b, frame_id=4)
    assert gate.mark_analyzed(analyzed_a.token)
    assert gate.observe(target_b, frame_id=5).changed
    assert gate.mark_analyzed(latest_b.token)
    assert not gate.observe(target_b, frame_id=6).changed
    assert not gate.mark_analyzed(analyzed_a.token)
    assert not gate.observe(target_b, frame_id=7).changed


def test_late_initial_ack_keeps_newer_scene_pending():
    gate = SceneChangeGate()
    initial = gate.observe(jpeg(scene()), frame_id=1)
    target = jpeg(with_target(scene()))
    gate.observe(target, frame_id=2)
    gate.observe(target, frame_id=3)
    assert gate.mark_analyzed(initial.token)
    assert gate.observe(target, frame_id=4).changed


def test_foreign_duplicate_and_retired_tokens_cannot_replace_baseline():
    gate, first = initialized()
    other, foreign = initialized()
    assert not gate.mark_analyzed(foreign.token)
    assert not gate.mark_analyzed(first.token)
    gate.reset()
    assert not gate.mark_analyzed(first.token)
    replacement = gate.observe(jpeg(scene()), frame_id='first')
    assert replacement.initial and replacement.changed
    assert gate.mark_analyzed(replacement.token)
    assert not other.mark_analyzed(replacement.token)


def test_reset_during_decode_does_not_seed_new_epoch(monkeypatch):
    gate, _first = initialized()
    prepare = gate._prepare
    entered, finish = Event(), Event()

    def slow(jpg):
        entered.set()
        assert finish.wait(5)
        return prepare(jpg)

    monkeypatch.setattr(gate, '_prepare', slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(gate.observe, jpeg(with_target(scene())), frame_id=1)
        assert entered.wait(5)
        gate.reset()
        finish.set()
        late = pending.result(timeout=5)
    assert not late.changed and not gate.mark_analyzed(late.token)
    assert gate.observe(jpeg(scene()), frame_id=2).initial


@pytest.mark.parametrize('content', [b'', b'not a jpeg', None, 'wrong type'])
def test_invalid_image_does_not_replace_last_successful_baseline(content):
    gate, _first = initialized()
    with pytest.raises(ValueError, match='invalid_scene_image'):
        gate.observe(content)
    assert not gate.observe(jpeg(scene()), frame_id='still-valid').changed


@pytest.mark.parametrize('kwargs', [
    {'pixel_threshold': 0}, {'pixel_threshold': float('nan')},
    {'global_fraction': 1.1}, {'local_fraction': 0},
    {'confirmations': 0}, {'blur_size': 4}, {'block_size': 0}, {'sample_size': (0, 1)},
])
def test_threshold_configuration_is_explicitly_validated(kwargs):
    with pytest.raises(ValueError):
        SceneChangeGate(**kwargs)
