from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Sequence

import gas_usage_interpolator as core


DEFAULT_STATISTIC_ID = 'water_estimator:usage'


def parse_reading_time(text: str) -> datetime:
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        text += 'T12:00'
    return core.parse_datetime_jst(text)


def validate_bill(bill: dict) -> tuple[core.MeterPoint, core.MeterPoint]:
    start = core.MeterPoint(parse_reading_time(bill['start']), float(bill['previous']), 'water:reading')
    end = core.MeterPoint(parse_reading_time(bill['end']), float(bill['current']), 'water:reading')
    core.validate_observations([start, end])
    if start.ts >= end.ts or start.value < 0 or end.value < start.value:
        raise ValueError('invalid water reading interval or meter values')
    usage = float(bill['usage'])
    core.require_finite(usage)
    if usage < 0 or not math.isclose(end.value - start.value, usage, rel_tol=0, abs_tol=1e-9):
        raise ValueError('water usage does not match current minus previous reading')
    return start, end


def build_series(bills: Sequence[dict]) -> tuple[list[core.MeterPoint], float]:
    if not bills:
        raise ValueError('no water billing history; enter a reading interval first')
    points = []
    previous_end = None
    baseline = None
    for bill in bills:
        start, end = validate_bill(bill)
        if baseline is None:
            baseline = start.value
        if previous_end is not None:
            if start.ts != previous_end.ts or not math.isclose(start.value, previous_end.value, rel_tol=0, abs_tol=1e-9):
                raise ValueError('water billing history must have contiguous times and matching boundary readings')
        segment = core.interpolate_between(start, end)
        if points and segment and points[-1].ts == segment[0].ts:
            segment = segment[1:]
        points.extend(segment)
        previous_end = end
    return core.validate_hourly_points(points), baseline


def load_history(path: Path, statistic_id: str) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or data.get('statistic_id') != statistic_id:
        raise ValueError('water history version or statistic ID mismatch')
    bills = data['bills']
    build_series(bills)
    return bills


def add_bill(bills: Sequence[dict], bill: dict) -> list[dict]:
    start, end = validate_bill(bill)
    for existing in bills:
        left, right = validate_bill(existing)
        if start.ts == left.ts and end.ts == right.ts:
            if start.value == left.value and end.value == right.value:
                return list(bills)
            raise ValueError('water interval already exists with different readings')
    result = [*bills, bill]
    build_series(result)
    return result


def atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    os.close(fd)
    try:
        write(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Record water billing readings and backfill hourly external statistics.')
    parser.add_argument('--start', help='previous reading date/time; date only means 12:00 JST')
    parser.add_argument('--end', help='current reading date/time; date only means 12:00 JST')
    parser.add_argument('--previous', type=float, help='previous absolute meter reading (m³)')
    parser.add_argument('--current', type=float, help='current absolute meter reading (m³)')
    parser.add_argument('--usage', type=float, help='optional billed usage (m³), checked against the readings')
    parser.add_argument('--history', type=Path, default=Path('water_billing_history.json'))
    parser.add_argument('--output', type=Path, default=Path('water_hourly_preview.csv'))
    parser.add_argument('--statistic-id', default=DEFAULT_STATISTIC_ID)
    parser.add_argument('--commit', action='store_true', help='write the complete saved history to PostgreSQL')
    args = parser.parse_args(argv)
    source = core.validate_statistic_id(args.statistic_id)
    if source != 'water_estimator':
        raise ValueError('water statistic ID must use the water_estimator: namespace')
    if args.history.resolve() == args.output.resolve():
        raise ValueError('history and preview must use different paths')
    fields = (args.start, args.end, args.previous, args.current)
    if any(value is not None for value in fields) or args.usage is not None:
        if any(value is None for value in fields):
            parser.error('--start, --end, --previous and --current must be supplied together')
    bills = load_history(args.history, args.statistic_id)
    if args.start is not None:
        bill = dict(start=parse_reading_time(args.start).isoformat(),
                    end=parse_reading_time(args.end).isoformat(),
                    previous=args.previous, current=args.current,
                    usage=args.usage if args.usage is not None else args.current - args.previous)
        bills = add_bill(bills, bill)
    points, baseline = build_series(bills)
    payload = json.dumps(dict(version=1, statistic_id=args.statistic_id, bills=bills), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    atomic_write(args.history, lambda path: path.write_text(payload, encoding='utf-8'))
    atomic_write(args.output, lambda path: core.write_hourly_csv(points, path, baseline))
    print(f'Water history: {len(bills)} intervals; baseline={baseline:.6f} m³')
    print(f'Generated {len(points)} hourly points: {points[0].ts.isoformat()} -> {points[-1].ts.isoformat()}')
    print(f'End state={points[-1].value:.6f}; sum={points[-1].value - baseline:.6f}')
    print(f'History: {args.history}; preview: {args.output}')
    if args.commit:
        core.commit_statistics(points, baseline, args.statistic_id, name='Water Usage Estimated')
    else:
        print('History and preview saved. PostgreSQL was not modified. Use --commit to write statistics.')
    return 0
