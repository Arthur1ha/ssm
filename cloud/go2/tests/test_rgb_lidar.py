"""Offline RGB/LiDAR alignment checks; no robot motion or model calls."""
import json
import math
import stat
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.perception import rgb_lidar
from cloud.go2.connection.sensors import Go2Sensors
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE, OCCUPIED


CAMERA = {
    "fx": 400, "fy": 400, "cx": 320, "cy": 180,
    "rotation_body_from_camera": [[0, 0, 1], [-1, 0, 0], [0, -1, 0]],
    "translation_body_from_camera_m": [0, 0, 0.1],
}


def test_odom_callback_preserves_camera_projection_pose():
    sensors = Go2Sensors()
    sensors._odom_session_id = "s"
    sensors._on_odom({"data": {
        "header": {"frame_id": "odom"},
        "pose": {"position": {"x": 1, "y": 2, "z": 0.4},
                 "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}},
    }})
    assert sensors.odom["z"] == 0.4
    assert sensors.odom["orientation"] == [0, 0, 0, 1]
    assert sensors.odom["session_id"] == "s"


def test_malformed_odom_packet_invalidates_pose_for_camera_projection():
    sensors = Go2Sensors()
    sensors._odom_session_id = "s"
    sensors._on_odom({"data": {"header": {"frame_id": "odom"},
                                "pose": {"position": {"x": 1, "y": 2, "z": 0.4},
                                         "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}})
    assert sensors.odom_received_at is not None
    sensors._on_odom({"data": {"header": {"frame_id": "odom"}, "pose": {"position": {"x": 3}}}})
    assert sensors.odom == {}
    assert sensors.odom_received_at is None
    assert sensors.sensor_diagnostics["pose"]["invalid_samples"] == 1
    assert sensors.sensor_diagnostics["pose"]["last_invalid_reason"] == "invalid_pose_values"


def test_project_odom_voxels_into_visual_target_box():
    pose = {"x": 0.0, "y": 0.0, "z": 0.4,
            "orientation": [0, 0, 0, 1]}
    points = np.array([[1.0, 0.0, 0.5], [1.0, -0.1, 0.5],
                       [-1.0, 0.0, 0.5], [float("nan"), 0.0, 0.0]])
    pixels, depths, visible = rgb_lidar.project_odom_points(points, pose, CAMERA, 640, 360)
    assert np.allclose(pixels, [[320, 180], [360, 180]])
    assert np.allclose(depths, [1, 1])
    assert np.allclose(visible, points[:2])
    assert rgb_lidar.bbox_depths(pixels, depths, [480, 450, 520, 550], 640, 360).tolist() == [1.0]


def test_projection_uses_full_odom_orientation():
    pose = {"x": 2.0, "y": 3.0, "z": 0.4,
            "orientation": [0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4)]}
    pixels, depth, visible = rgb_lidar.project_odom_points(
        np.array([[2.0, 4.0, 0.5]]), pose, CAMERA, 640, 360)
    assert np.allclose(pixels, [[320, 180]])
    assert np.allclose(depth, [1])
    assert np.allclose(visible, [[2.0, 4.0, 0.5]])


def test_target_map_point_selects_reachable_free_stop_cell():
    grid = OccupancyGrid.__new__(OccupancyGrid)
    grid.origin = [-2.0, -2.0]
    grid.width = [80, 80]
    grid.resolution = 0.05
    grid.cells = np.full((80, 80), FREE, dtype=np.uint8)
    grid.obstacles = np.zeros((80, 80), dtype=bool)
    grid.known_floor = np.ones((80, 80), dtype=bool)
    grid.has_free_space_evidence = True
    target_cell = grid.odom_to_grid(1.0, 0.0)
    grid.cells[target_cell[1], target_cell[0]] = OCCUPIED
    grid.obstacles[target_cell[1], target_cell[0]] = True
    grid.grid = grid.cells != FREE
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
    stop = rgb_lidar.select_reachable_stop(grid, pose, (1.0, 0.0))
    assert stop is not None and not stop["already_near"]
    assert 0.85 <= stop["distance_to_target_m"] <= 0.93
    assert pose["x"] < stop["x"] < 1.0
    assert grid.is_body_clear(stop["x"], stop["y"], stop["heading"])
    near = rgb_lidar.select_reachable_stop(
        grid, {"x": 0.15, "y": 0.0, "heading": 0.0}, (1.0, 0.0))
    assert near is not None and near["already_near"]
    assert rgb_lidar.select_reachable_stop(grid, pose, (10.0, 0.0)) is None


def test_tabletop_above_robot_does_not_block_body_but_low_obstacle_does():
    floor = [[x, y, 0.0] for x in np.arange(-0.8, 0.81, 0.05)
             for y in np.arange(-0.8, 0.81, 0.05)]
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0}

    def grid_with_obstacle(height):
        return OccupancyGrid({"data": {
            "frame_id": "odom", "resolution": 0.05,
            "origin": [-1.0, -1.0, -0.5], "width": [40, 40, 40],
            "data": {"points": floor + [[0.35, 0.0, height]]},
        }}, robot_pose=pose)

    assert grid_with_obstacle(1.1).is_body_clear(0.0, 0.0, 0.0)
    assert not grid_with_obstacle(0.4).is_body_clear(0.0, 0.0, 0.0)


def test_visual_box_object_returns_exclude_measured_floor():
    points = np.array([[2.7, -1.1, -0.05], [2.3, -0.85, 0.12],
                       [2.3, -0.8, 0.22]])
    in_box = np.array([True, True, True])
    assert rgb_lidar.object_returns_in_box(points, in_box, -0.05).tolist() == [False, True, True]
    assert not rgb_lidar.object_returns_in_box(points, in_box, None).any()


def test_paired_capture_preserves_metric_points_and_frame_pose(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(rgb_lidar, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    image = tmp_path / "vision.jpg"
    image.write_bytes(b"jpeg")
    pose = {"x": 0.0, "y": 0.0, "z": 0.4, "orientation": [0, 0, 0, 1],
            "session_id": "s"}
    frame = {"frame_id": 7, "width": 640, "height": 360,
             "received_at": 10.0, "pose_received_at": 9.95,
             "odom_session_id": "s", "pose_at_capture": pose}
    voxel = {"data": {"frame_id": "odom", "resolution": 0.05,
                      "data": {"points": np.array([[1.0, 0.0, 0.5], [5.0, 0.0, 0.5]])}}}
    rgb_lidar.save_paired_sample(image, frame, voxel, 10.05,
                                 [450, 400, 550, 600])
    sample = tmp_path / "vision.npz"
    assert stat.S_IMODE(sample.stat().st_mode) == 0o600
    with np.load(sample, allow_pickle=False) as data:
        assert np.allclose(data["points_odom"], [[1.0, 0.0, 0.5]])
        metadata = json.loads(str(data["metadata"]))
    assert metadata["pose_at_capture"] == pose
    assert math.isclose(metadata["voxel_skew_s"], 0.05)
    assert events[-2][1]["saved"] is True
    assert events[-1][1]["phase"] == "nominal_projection"
    assert events[-1][1]["points_in_target_box"] == 1
    assert events[-1][1]["candidate_odom_xy"] is None


def test_lidar_capture_event_is_allowed_by_task_log_contract(monkeypatch):
    from cloud.observability import events

    written = []
    monkeypatch.setattr(events, "write_agent_event", written.append)
    events.emit_agent_event("vision.lidar", phase="capture", saved=False,
                            reason="voxel_unavailable")
    assert written[0]["event"] == "vision.lidar"
    assert written[0]["reason"] == "voxel_unavailable"


def test_missing_3d_pose_does_not_create_fusion_sample(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(rgb_lidar, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    frame = {"frame_id": 1, "received_at": 10.0,
             "odom_session_id": "s", "pose_at_capture": {"session_id": "s"}}
    voxel = {"data": {"header": {"frame_id": "odom"},
                      "data": {"points": [[1, 0, 0]]}}}
    rgb_lidar.save_paired_sample(tmp_path / "frame.jpg", frame, voxel, 10.0)
    assert not (tmp_path / "frame.npz").exists()
    assert events[-1][1]["reason"] == "pose_3d_unavailable"


def test_voxel_time_offset_is_recorded_without_blocking_localization(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(rgb_lidar, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")
    frame = {"frame_id": 2, "received_at": 10.0, "odom_session_id": "s",
             "pose_at_capture": {"session_id": "s", "x": 0, "y": 0, "z": 0.3,
                                 "orientation": [0, 0, 0, 1]}}
    voxel = {"data": {"frame_id": "odom", "data": {"points": [[1, 0, 0]]}}}
    sample = rgb_lidar.save_paired_sample(image, frame, voxel, 6.38)
    assert sample == image.with_suffix(".npz")
    with np.load(sample, allow_pickle=False) as data:
        metadata = json.loads(str(data["metadata"]))
    assert math.isclose(metadata["voxel_skew_s"], -3.62)
    assert events[0][1]["saved"] is True


@pytest.mark.asyncio
async def test_target_capture_endpoint_collects_pair_without_motion(tmp_path, monkeypatch):
    from cloud.go2 import router
    from cloud.go2.perception import visual_target

    frame = {"frame_id": 9, "received_at": 10.0, "width": 640, "height": 360}
    robot = SimpleNamespace(is_connected=True,
                            latest_frame_snapshot=lambda: (b"jpeg", frame),
                            voxel_raw={"data": {}}, voxel_received_at=10.05)
    monkeypatch.setattr(router, "go2", robot)
    monkeypatch.setattr(visual_target, "save_diagnostic_frame",
                        lambda *_: tmp_path / "frame.jpg")

    async def detect(_jpeg, _target, _frame):
        return {"found": True, "bbox_1000": [450, 400, 550, 600]}

    monkeypatch.setattr(visual_target, "detect_visible_target", detect)
    monkeypatch.setattr(rgb_lidar, "save_paired_sample",
                        lambda *_: tmp_path / "frame.npz")
    result = await router.capture_rgb_lidar_target("毛绒玩具", context=object())
    assert result["observation"]["found"] is True
    assert result["lidar_artifact"] == str(tmp_path / "frame.npz")
    assert result["motion_command_sent"] is False


def test_upstream_profile_is_explicitly_unmeasured_and_scales_only_same_fov():
    small = rgb_lidar.go2_camera_profile(640, 360)
    large = rgb_lidar.go2_camera_profile(1280, 720)
    assert small['fx'] * 2 == large['fx']
    assert small['distortion_coefficients'] == large['distortion_coefficients']
    assert '3645017c7ace59badd938283b30439a83545fcd7' in small['source_url']
    assert small['measurement_status'] == 'upstream_model_not_per_robot_calibration'
    assert not small.get('measured_on_robot') and not small.get('replay_verified')
    with pytest.raises(ValueError, match='resolution_mismatch'):
        rgb_lidar.go2_camera_profile(640, 480)


def test_equidistant_projection_uses_angle_not_pinhole_radius():
    camera = dict(CAMERA, distortion_model='equidistant', distortion_coefficients=[0.] * 4)
    pose = {'x': 0., 'y': 0., 'z': .4, 'orientation': [0, 0, 0, 1]}
    pixels, _, _ = rgb_lidar.project_odom_points(np.array([[1., -.5, .5]]), pose, camera, 640, 360)
    assert pixels[0, 0] == pytest.approx(320 + 400 * math.atan(.5))
    box = [pixels[0, 0] / 640 * 1000 - 1, 499, pixels[0, 0] / 640 * 1000 + 1, 501]
    assert rgb_lidar.image_bearing(camera, box, 640, 360) == pytest.approx(-math.atan(.5))


def _lidar_association(points, bbox=(450, 450, 550, 850)):
    pose = {'x': 0., 'y': 0., 'z': .4, 'orientation': [0, 0, 0, 1]}
    return rgb_lidar.associate_lidar_target(
        {'data': {'resolution': .05, 'data': {'points': points}}}, pose, CAMERA,
        {'width': 640, 'height': 360}, list(bbox), 0.)


def test_lidar_cluster_uses_foreground_range_not_background_or_floor_median():
    toy = [[2., 0., z] for z in (.15, .20, .25, .30)]
    floor = [[x, y, 0.] for x in (1., 2., 3.) for y in (-.1, 0., .1)]
    wall = [[3.8, y, z] for y in np.arange(-.8, .81, .05)
            for z in np.arange(.1, 1., .05)]
    result = _lidar_association(toy + floor)
    assert result['x'] == 2. and result['y'] == 0.
    assert result['lidar_points'] >= 3
    assert result['cluster_points'] == 4
    assert result['uncertainty_m'] > .15
    assert _lidar_association(toy + floor + wall)['x'] == 2.
    assert _lidar_association(floor) is None


def test_lidar_cluster_rejects_ambiguous_objects_and_tiny_wall_crop():
    toy = [[2., y, z] for y in (-.1, .1) for z in (.15, .20, .25, .30)]
    assert _lidar_association(toy) is None
    wall = [[2., y, z] for y in np.arange(-.5, .51, .05)
            for z in np.arange(.1, .61, .05)]
    assert _lidar_association(wall, bbox=(490, 600, 510, 720)) is None
    assert _lidar_association([[2., 0., .15], [2., 0., .20]]) is None


def test_lidar_bbox_tolerates_small_pixel_error_without_choosing_distant_cluster():
    toy = [[2., 0., z] for z in (.15, .20, .25, .30)]
    # Projection is x320; the left box edge is x324, a four-pixel model error.
    assert _lidar_association(toy, bbox=(506.25, 600, 540, 720)) is not None
    assert _lidar_association(toy, bbox=(540, 600, 570, 720)) is None


def test_short_floor_object_is_not_erased_by_navigation_obstacle_height():
    # These points are above the measured floor band but below the navigation
    # obstacle cutoff. Float32 voxel subtraction also spans exactly one voxel.
    points = np.array([[2., 0., .10], [2., 0., .125], [2., 0., .15]], dtype=np.float32)
    assert _lidar_association(points, bbox=(470, 650, 530, 740)) is not None
    floor = np.array([[2., y, z] for y in (-.05, 0., .05) for z in (0., .025, .075)])
    assert _lidar_association(floor, bbox=(470, 650, 530, 850)) is None


def test_one_voxel_vertical_extent_survives_float32_rounding():
    points = np.array([[2., 0., .175], [2., 0., .20], [2., 0., .225]], dtype=np.float32)
    assert float(np.ptp(points[:, 2])) < .05  # representational error, not flatness
    assert _lidar_association(points, bbox=(470, 600, 530, 740)) is not None


def test_association_diagnostics_distinguish_background_from_missing_returns():
    pose = {'x': 0., 'y': 0., 'z': .4, 'orientation': [0, 0, 0, 1]}
    frame = {'width': 640, 'height': 360}
    report = {}
    wall = [[2., y, z] for y in np.arange(-.5, .51, .05) for z in np.arange(.1, .61, .05)]
    assert rgb_lidar.associate_lidar_target({'data': {'resolution': .05, 'data': {'points': wall}}},
        pose, CAMERA, frame, [490, 600, 510, 720], 0., diagnostics=report) is None
    assert report['foreground_in_padded_box'] >= 3
    assert report['reason'] == 'no_isolated_foreground_component'
    assert 'component_not_compact' in report['components'][0]['rejected_by']
    assert 'component_mostly_outside_box' in report['components'][0]['rejected_by']
    report = {}
    assert rgb_lidar.associate_lidar_target({'data': {'data': {'points': [[2., 0., 0.]]}}},
        pose, CAMERA, frame, [490, 600, 510, 720], 0., diagnostics=report) is None
    assert report['reason'] == 'insufficient_foreground_returns'
    assert report['foreground_in_padded_box'] == 0


def test_paired_capture_embeds_exact_image_sha(tmp_path):
    import hashlib
    image = tmp_path / 'frame.jpg'
    image.write_bytes(b'exact submitted image')
    pose = {'x': 0., 'y': 0., 'z': .3, 'orientation': [0, 0, 0, 1], 'session_id': 's'}
    frame = {'frame_id': 1, 'width': 640, 'height': 360, 'received_at': 1.,
             'odom_session_id': 's', 'pose_at_capture': pose}
    voxel = {'data': {'frame_id': 'odom', 'data': {'points': [[1., 0., 0.]]}}}
    path = rgb_lidar.save_paired_sample(image, frame, voxel, 1.)
    with np.load(path, allow_pickle=False) as data:
        meta = json.loads(str(data['metadata']))
    assert meta['image_sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()


def test_offline_replay_requires_matching_frame_image_sha_and_bbox(tmp_path):
    import hashlib
    from scripts.replay_go2_lidar_target import index_events, replay
    image = tmp_path / 'run-7-abc.jpg'
    image.write_bytes(b'jpeg')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    log = tmp_path / 'events.jsonl'
    entries = [
        {'run_id': 'run', 'frame_id': 7, 'event': 'vision.observe', 'phase': 'frame_capture',
         'saved': True, 'sha256': digest},
        {'run_id': 'run', 'frame_id': 8, 'event': 'vision.model', 'phase': 'parsed',
         'observation': {'bbox_1000': [400, 400, 600, 600]}},
    ]
    log.write_text('\n'.join(json.dumps(row) for row in entries))
    assert replay(image, index_events([log]))['reason'] == 'bbox_event_unavailable'
    entries[1]['frame_id'] = 7
    log.write_text('\n'.join(json.dumps(row) for row in entries))
    result = replay(image, index_events([log]))
    assert result['reason'] == 'point_pair_unavailable'
    assert result['image_sha256'] == digest
    assert result['bbox_event'] == str(log) + ':2'
    image.write_bytes(b'another jpeg')
    assert replay(image, index_events([log]))['reason'] == 'image_sha_not_linked_to_events'
