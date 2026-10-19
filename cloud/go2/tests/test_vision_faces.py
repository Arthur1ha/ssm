import cv2
import numpy as np
import pytest

import cloud.go2.agentcore.skills.vision as vision


def jpeg(width=640, height=360):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = (20, 40, 80)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def frame(boxes):
    return {
        "frame_id": 1, "ts": 0.0,
        "persons": {"detected": False, "count": 0},
        "faces": {"detected": bool(boxes), "count": len(boxes), "boxes": boxes},
        "changed": False, "change_type": "none",
    }


def test_detect_maps_boxes_to_non_4_3_original(monkeypatch):
    class Hog:
        def detectMultiScale(self, *args, **kwargs):
            return (), ()

    class Cascade:
        def detectMultiScale(self, *args, **kwargs):
            return np.array([[10, 20, 50, 60]])

    monkeypatch.setattr(vision, "_get_hog", lambda: Hog())
    monkeypatch.setattr(vision, "_get_face_cascade", lambda: Cascade())
    result = vision.detect(jpeg(640, 360), frame_id=9)
    assert result["frame_id"] == 9
    assert result["faces"]["boxes"] == [(20, 30, 100, 90)]


def test_crop_padding_is_clamped_at_edges():
    cropped = vision.crop_face_jpeg(jpeg(200, 100), (0, 0, 80, 80), padding=0.2)
    image = cv2.imdecode(np.frombuffer(cropped, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[1] == 96
    assert image.shape[0] == 96


def test_enrollment_requires_one_large_face():
    data = jpeg()
    with pytest.raises(vision.VisionFaceRejected) as exc:
        vision.select_enrollment_face(frame([]), data)
    assert exc.value.code == "no_face"
    with pytest.raises(vision.VisionFaceRejected) as exc:
        vision.select_enrollment_face(frame([(0, 0, 100, 100), (120, 0, 100, 100)]), data)
    assert exc.value.code == "multiple_faces"
    with pytest.raises(vision.VisionFaceRejected) as exc:
        vision.select_enrollment_face(frame([(0, 0, 79, 100)]), data)
    assert exc.value.code == "face_too_small"
    assert vision.select_enrollment_face(frame([(0, 0, 100, 100)]), data).startswith(b"\xff\xd8")


def test_vision_loop_frame_cache_is_bounded():
    loop = vision.VisionLoop()
    loop._frames[1] = b"1"
    loop._frames[2] = b"2"
    loop._frames[3] = b"3"
    while len(loop._frames) > 2:
        loop._frames.popitem(last=False)
    assert loop.get_frame(1) is None
    assert loop.get_frame(3) == b"3"
