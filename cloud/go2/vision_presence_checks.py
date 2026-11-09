"""Face and body detections share one person-presence observation."""

import cv2
import numpy as np
import pytest

from cloud.go2.perception import vision


@pytest.fixture
def detection_frame(monkeypatch):
    detections = {'bodies': 0, 'faces': 0}

    class BodyDetector:
        def detectMultiScale(self, *args, **kwargs):
            return [(0, 0, 64, 128)] * detections['bodies'], ()

    class FaceDetector:
        def detectMultiScale(self, *args, **kwargs):
            return [(10, 20, 40, 40)] * detections['faces']

    monkeypatch.setattr(vision, '_get_hog', BodyDetector)
    monkeypatch.setattr(vision, '_get_face_cascade', FaceDetector)
    ok, encoded = cv2.imencode('.jpg', np.zeros((360, 640, 3), dtype=np.uint8))
    assert ok
    return detections, encoded.tobytes()


@pytest.mark.parametrize('bodies,faces,count', [(0, 0, 0), (1, 0, 1), (0, 1, 1),
                                               (1, 1, 1), (1, 2, 2), (2, 1, 2)])
def test_either_detector_reports_people_without_adding_duplicate_counts(detection_frame, bodies, faces, count):
    detections, jpeg = detection_frame
    detections.update(bodies=bodies, faces=faces)
    result = vision.detect(jpeg, prev_person_count=0)
    assert result['persons'] == {'detected': count > 0, 'count': count}
    assert result['faces']['count'] == faces
    assert result['faces']['detected'] is (faces > 0)
    assert result['changed'] is (count > 0)
    assert result['change_type'] == ('person_entered' if count else 'none')


def test_switching_between_face_and_body_is_one_continuous_appearance(detection_frame):
    detections, jpeg = detection_frame
    previous = 0
    for index, (bodies, faces) in enumerate(((0, 1), (1, 1), (1, 0), (0, 1), (0, 0))):
        detections.update(bodies=bodies, faces=faces)
        result = vision.detect(jpeg, prev_person_count=previous, frame_id=index)
        previous = result['persons']['count']
        assert result['change_type'] == ('person_entered' if index == 0 else
                                         'person_left' if index == 4 else 'none')
        assert result['changed'] is (index in (0, 4))


def test_body_detection_does_not_create_identity_evidence(detection_frame):
    detections, jpeg = detection_frame
    detections['bodies'] = 1
    result = vision.detect(jpeg)
    assert result['persons']['detected'] is True
    assert result['faces'] == {'detected': False, 'count': 0, 'boxes': []}
