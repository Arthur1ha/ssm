"""Replay saved target association only; no connection, model or motion imports.

uv run python scripts/replay_go2_lidar_target.py logs/vision/site_owl_*.npz \
    --events logs/session-go2-acceptance-*.jsonl
JPEG inputs also report missing point pairs. JSON output links each bbox to its
original event and SHA-256 image, and explains each association rejection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, Z_FLOOR_M
from cloud.go2.perception.rgb_lidar import (
    associate_lidar_target, bbox_mask, go2_camera_profile, nominal_camera, project_odom_points,
)


def index_events(paths: list[Path]) -> dict:
    records = {}
    for path in paths:
        for line, raw in enumerate(path.read_text().splitlines(), 1):
            event = json.loads(raw)
            key = (event.get('run_id'), event.get('frame_id'))
            record = records.setdefault(key, {'captures': [], 'boxes': []})
            source = f'{path}:{line}'
            if event.get('event') == 'vision.observe' and event.get('phase') == 'frame_capture' and event.get('saved'):
                record['captures'].append({'sha256': event.get('sha256'), 'source': source})
            if event.get('event') == 'vision.model' and event.get('phase') == 'parsed':
                box = (event.get('observation') or {}).get('bbox_1000')
                if box is not None:
                    record['boxes'].append({'bbox_1000': box, 'source': source})
    return records


def replay(sample_path: Path, events: dict, *, compare_nominal=False) -> dict:
    pair_path = sample_path.with_suffix('.npz')
    image_path = sample_path.with_suffix('.jpg')
    run_id, frame_text, _ = sample_path.stem.rsplit('-', 2)
    frame_id = int(frame_text)
    result = {'run_id': run_id, 'frame_id': frame_id, 'sample': str(pair_path),
              'image': str(image_path), 'localization': None}
    if not image_path.is_file():
        return dict(result, reason='image_unavailable')
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    result['image_sha256'] = digest
    evidence = events.get((run_id, frame_id), {'captures': [], 'boxes': []})
    captures = [entry for entry in evidence['captures'] if entry['sha256'] == digest]
    if not captures:
        return dict(result, reason='image_sha_not_linked_to_events')
    result['image_event'] = captures[0]['source']
    boxes = evidence['boxes']
    if not boxes:
        return dict(result, reason='bbox_event_unavailable')
    if any(entry['bbox_1000'] != boxes[0]['bbox_1000'] for entry in boxes):
        return dict(result, reason='bbox_events_disagree')
    result.update(bbox_1000=boxes[0]['bbox_1000'], bbox_event=boxes[0]['source'])
    if not pair_path.is_file():
        return dict(result, reason='point_pair_unavailable')
    result['sample_sha256'] = hashlib.sha256(pair_path.read_bytes()).hexdigest()
    with np.load(pair_path, allow_pickle=False) as sample:
        metadata = json.loads(str(sample['metadata']))
        points = sample['points_odom'].copy()
    if metadata.get('image_sha256') not in (None, digest):
        return dict(result, reason='pair_image_sha_mismatch')
    result['pair_image_link'] = ('embedded_sha256' if metadata.get('image_sha256') else
                                 'legacy_filename_and_capture_event_sha256')
    pose = metadata.get('pose_at_capture') or {}
    if (metadata.get('frame_id') != frame_id or metadata.get('voxel_frame_id') != 'odom'
            or not metadata.get('odom_session_id')
            or pose.get('session_id') != metadata.get('odom_session_id')):
        return dict(result, reason='pair_frame_or_session_mismatch')
    pose_skew = metadata['image_received_at'] - metadata['pose_received_at']
    cloud_skew = metadata['voxel_received_at'] - metadata['image_received_at']
    result.update(pose_skew_s=pose_skew, voxel_skew_s=cloud_skew,
                  odom_session_id=metadata['odom_session_id'])
    if any(not math.isfinite(skew) or abs(skew) > .25 for skew in (pose_skew, cloud_skew)):
        return dict(result, reason='sensor_frame_skew')
    voxel = {'data': {'frame_id': 'odom', 'origin': metadata['voxel_origin'],
        'width': metadata['voxel_width'], 'resolution': metadata['voxel_resolution_m'],
        'data': {'points': points}}}
    floor = OccupancyGrid(voxel, robot_pose=pose).measured_floor_z
    width, height = metadata['width'], metadata['height']
    camera = go2_camera_profile(width, height)
    result.update(camera_model=camera['profile'], camera_source=camera['source_url'],
        camera_measurement_status=camera['measurement_status'], floor_z_m=floor)
    diagnostics = {}
    result['localization'] = associate_lidar_target(voxel, pose, camera, metadata,
        result['bbox_1000'], floor, diagnostics=diagnostics)
    result['association'] = diagnostics
    result['reason'] = diagnostics['reason']
    # Compare the previous obstacle-height gate without changing the production
    # camera model or using the old nominal model to authorize a target.
    comparisons = {}
    for name, profile in [('dimos', camera)] + ([('unverified_URDF90', nominal_camera(width, height))]
                                              if compare_nominal else []):
        pixels, depths, world = project_odom_points(points, pose, profile, width, height)
        exact = bbox_mask(pixels, result['bbox_1000'], width, height)
        keep = ((world[:, 2] > floor + Z_FLOOR_M) & (depths <= 4.)) if floor is not None else np.zeros(len(world), bool)
        comparisons[name] = {'legacy_obstacle_floor_band_m': Z_FLOOR_M,
                            'legacy_foreground_exact_box': int(np.count_nonzero(exact & keep))}
    result['diagnostic_comparison'] = comparisons
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples', nargs='+', type=Path)
    parser.add_argument('--events', nargs='+', type=Path, required=True)
    parser.add_argument('--compare-nominal', action='store_true')
    args = parser.parse_args()
    records = index_events(args.events)
    print(json.dumps([replay(path, records, compare_nominal=args.compare_nominal)
                      for path in args.samples], ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
