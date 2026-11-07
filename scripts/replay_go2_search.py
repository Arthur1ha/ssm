"""Replay saved paired Go2 samples; never connect to a robot or dispatch motion.

uv run python scripts/replay_go2_search.py logs/vision/codex_turn_*.npz
Samples are the existing private NPZ/JPEG pairs produced by capture diagnostics.
They are discrete observations, not a complete continuous sensor recording.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_sample(path: Path) -> tuple[dict, dict]:
    with np.load(path, allow_pickle=False) as sample:
        metadata = json.loads(str(sample['metadata']))
        points = np.asarray(sample['points_odom'], dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError('invalid point sample')
    if metadata.get('voxel_frame_id') != 'odom' or not metadata.get('odom_session_id'):
        raise ValueError('missing or unsupported coordinate session')
    return metadata, {'data': {
        'frame_id': 'odom', 'resolution': metadata['voxel_resolution_m'],
        'origin': metadata['voxel_origin'], 'width': metadata['voxel_width'],
        'data': {'points': points},
    }}


def replay(paths: list[Path], bounds: tuple[float, float, float, float] | None = None) -> list[dict]:
    from cloud.go2.navigation.session_map import SessionMap
    from cloud.go2.navigation.observation_coverage import ObservationCoverage
    from cloud.go2.navigation.frontier import select_observation_target

    samples = [(path, *load_sample(path)) for path in paths]
    samples.sort(key=lambda row: (row[1]['odom_session_id'], row[1]['voxel_received_at']))
    mapping = SessionMap()
    rows = []
    coverage = None
    session = None
    for path, metadata, voxel in samples:
        if session != metadata['odom_session_id']:
            session = metadata['odom_session_id']
            mapping.reset(session)
            coverage = ObservationCoverage(session)
        started = time.perf_counter()
        grid = mapping.update(voxel, metadata['pose_at_capture'], session_id=session,
                              observed_at=metadata['voxel_received_at'])
        build_ms = (time.perf_counter() - started) * 1000
        if grid is None:
            rows.append({'sample': str(path), 'reason': 'sample_rejected', 'build_ms': build_ms})
            continue
        started = time.perf_counter()
        selection = select_observation_target(grid, metadata['pose_at_capture'], coverage, bounds=bounds)
        planning_ms = (time.perf_counter() - started) * 1000
        target = selection.target
        rows.append({
            'sample': str(path), 'image': str(path.with_suffix('.jpg')),
            'map_session': session, 'map_version': grid.version,
            'observation_time': metadata['voxel_received_at'],
            'image_voxel_skew_s': metadata.get('voxel_skew_s'),
            'point_count': len(voxel['data']['data']['points']),
            'build_ms': round(build_ms, 3), 'planning_ms': round(planning_ms, 3),
            'reason': selection.reason,
            'target': asdict(target) if is_dataclass(target) else target,
            'free_cells': int(np.count_nonzero(grid.cells == 1)),
            'occupied_cells': int(np.count_nonzero(grid.cells == 2)),
            'unknown_cells': int(np.count_nonzero(grid.cells == 0)),
            'coverage': coverage.summary(grid),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples', nargs='+', type=Path)
    parser.add_argument('--bounds', nargs=4, type=float, metavar=('XMIN', 'YMIN', 'XMAX', 'YMAX'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.bounds and not (args.bounds[0] < args.bounds[2] and args.bounds[1] < args.bounds[3]):
        parser.error('bounds must have positive area')
    rows = replay(args.samples, tuple(args.bounds) if args.bounds else None)
    result = json.dumps({'motion_sent': False, 'source': 'discrete_paired_samples',
                         'observations_marked_searched': False, 'frames': rows},
                        ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + '\n')
        args.output.chmod(0o600)
    print(result)


if __name__ == '__main__':
    main()
