#!/usr/bin/env python3
# Based on VERSION: 2026-09-04-v4; external statistics edition
"""
Home Assistant gas usage interpolator / statistics backfiller.

Usage
=====

CSVを解析して、1時間按分のプレビューを作る:
    python3 gas_usage_interpolator.py gas/0904.csv

CSVを解析して Home Assistant の PostgreSQL statistics に反映:
    python3 gas_usage_interpolator.py gas/0904.csv --commit

現在の実ガスメーター値を記録し、
直前の既知点から現在までを1時間単位で按分:
    python3 gas_usage_interpolator.py 459.439

観測時刻を明示:
    python3 gas_usage_interpolator.py 459.439 --at 2026-09-04T13:45

手動観測の按分結果も PostgreSQL に反映:
    python3 gas_usage_interpolator.py 459.439 --at 2026-09-04T13:45 --commit

重要な考え方
============

- gas_estimator:usage の state はメーター絶対値、sum は基準点からの使用量。
- CSVは期間内の増加量だけを持つ。
- 手動観測は、その時刻のガスメーター絶対値。
- CSV期間は:
      開始日 12:00 JST <= t < 終了日の翌日 12:00 JST
- CSV期間内に手動観測があれば、その観測値をアンカーとして再按分。
- 最新CSVの終了後に手動観測した場合は、
      最新CSV終端 -> 手動観測時刻
  を線形按分して現在まで統計を延長する。
- 将来その期間を含むCSVが来たら、CSV総量を正として再按分する。
- PostgreSQLへの書き込みは --commit のときだけ。
- statistics_short_term は触らない。
- 通常sensor/helper、Home Assistant REST APIには依存しない。
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


# ============================================================
# 固定設定
# ============================================================

JST = ZoneInfo("Asia/Tokyo")
BOUNDARY_TIME = time(12, 0)

# 新居のみの信頼できる起点
DEFAULT_ANCHOR_TIME = "2026-06-25T12:00"
DEFAULT_ANCHOR_VALUE = 412.0

DEFAULT_OBSERVATIONS_FILE = Path("gas_manual_observations.csv")
DEFAULT_OUTPUT_FILE = Path("gas_hourly_preview.csv")

DEFAULT_STATISTIC_ID = "gas_estimator:usage"


# ============================================================
# データ型
# ============================================================

@dataclass(frozen=True)
class MeterPoint:
    ts: datetime
    value: float
    source: str = "unknown"


@dataclass(frozen=True)
class BillingPeriod:
    bill_month: str
    start_date: date
    end_date: date
    usage_m3: float
    days: int | None = None

    @property
    def start_ts(self) -> datetime:
        return datetime.combine(
            self.start_date,
            BOUNDARY_TIME,
            tzinfo=JST,
        )

    @property
    def end_ts(self) -> datetime:
        # CSVの両端日を含む。
        # 内部表現は [start, end)。
        return datetime.combine(
            self.end_date + timedelta(days=1),
            BOUNDARY_TIME,
            tzinfo=JST,
        )


JP_PERIOD_RE = re.compile(
    r"(?P<sy>\d{4})年(?P<sm>\d{1,2})月(?P<sd>\d{1,2})日"
    r"[～〜~-]"
    r"(?P<ey>\d{4})年(?P<em>\d{1,2})月(?P<ed>\d{1,2})日"
)


# ============================================================
# 共通
# ============================================================

def require_finite(value: float) -> None:
    if not math.isfinite(value):
        raise ValueError("meter and usage values must be finite")


def validate_observations(points: Sequence[MeterPoint]) -> None:
    seen = set()
    for point in points:
        require_finite(point.value)
        if point.ts > datetime.now(JST):
            raise ValueError("future observations are not supported")
        if point.ts in seen:
            raise ValueError(f"duplicate observation timestamp: {point.ts.isoformat()}")
        seen.add(point.ts)
    ordered = sorted(points, key=lambda point: point.ts)
    if any(b.value < a.value for a, b in zip(ordered, ordered[1:])):
        raise ValueError("manual meter values must not decrease")


def parse_datetime_jst(text: str) -> datetime:
    ts = datetime.fromisoformat(text)

    if ts.tzinfo is None:
        return ts.replace(tzinfo=JST)

    return ts.astimezone(JST)


def looks_like_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def is_exact_hour(ts: datetime) -> bool:
    return (
        ts.minute == 0
        and ts.second == 0
        and ts.microsecond == 0
    )


# ============================================================
# 大阪ガスCSV
# ============================================================

def decode_csv_file(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()

    encodings = (
        "utf-8-sig",
        "utf-8",
        "cp932",
        "shift_jis",
    )

    errors: list[str] = []

    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")

    raise UnicodeError(
        f"CSV encoding could not be detected: {path}\n"
        + "\n".join(errors)
    )


def parse_jp_period(text: str) -> tuple[date, date] | None:
    text = (text or "").strip()

    match = JP_PERIOD_RE.fullmatch(text)
    if not match:
        return None

    return (
        date(
            int(match["sy"]),
            int(match["sm"]),
            int(match["sd"]),
        ),
        date(
            int(match["ey"]),
            int(match["em"]),
            int(match["ed"]),
        ),
    )


def load_osakagas_csv(
    path: Path,
) -> tuple[list[BillingPeriod], list[dict[str, str]], str]:

    text, encoding = decode_csv_file(path)
    stream = io.StringIO(text)

    # 大阪ガスCSVの先頭説明行
    next(stream, None)

    reader = csv.DictReader(stream)

    periods: list[BillingPeriod] = []
    skipped: list[dict[str, str]] = []

    for row in reader:
        bill_month = (row.get("ご請求月") or "").strip()
        period_text = (row.get("ガス使用期間") or "").strip()
        usage_text = (row.get("ガス使用量(m3)") or "").strip()
        days_text = (row.get("ガス使用日数(日)") or "").strip()

        parsed = parse_jp_period(period_text)

        if parsed is None:
            skipped.append(row)
            continue

        if not usage_text or usage_text == "-":
            skipped.append(row)
            continue

        start_date, end_date = parsed
        usage_m3 = float(usage_text)
        require_finite(usage_m3)
        if usage_m3 < 0 or end_date < start_date:
            raise ValueError("invalid billing period or negative usage")

        days = int(days_text) if days_text.isdigit() else None

        inclusive_days = (end_date - start_date).days + 1

        if days is not None and days != inclusive_days:
            raise ValueError(
                f"{bill_month}: CSV days={days}, "
                f"but date range implies {inclusive_days} days"
            )

        periods.append(
            BillingPeriod(
                bill_month=bill_month,
                start_date=start_date,
                end_date=end_date,
                usage_m3=usage_m3,
                days=days,
            )
        )

    periods.sort(key=lambda period: period.start_ts)

    return periods, skipped, encoding


# ============================================================
# 手動ガスメーター観測
# ============================================================

def load_manual_observations(path: Path) -> list[MeterPoint]:
    if not path.exists():
        return []

    points: list[MeterPoint] = []

    with path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        for row in reader:
            points.append(
                MeterPoint(
                    ts=parse_datetime_jst(row["timestamp_jst"]),
                    value=float(row["meter_value_m3"]),
                    source=row.get("source") or "manual",
                )
            )

    points.sort(key=lambda point: point.ts)
    return points


def save_manual_observation(
    value: float,
    ts: datetime,
    observations_file: Path,
) -> bool:
    """
    保存したら True。
    同一時刻・同一値が既にある場合は False（冪等）。
    """
    validate_observations([MeterPoint(ts, value)])
    previous = load_manual_observations(observations_file)
    validate_observations(previous)

    for point in previous:
        if point.ts == ts:
            if abs(point.value - value) < 1e-9:
                return False
            raise ValueError(
                f"manual observation already exists at {ts.isoformat()} "
                f"with different value: {point.value:.6f}"
            )

    if previous:
        last = previous[-1]

        if ts < last.ts:
            raise ValueError(
                "new observation time must not be older than "
                "the latest manual observation: "
                f"{last.ts.isoformat()}"
            )

        if value < last.value:
            raise ValueError(
                "gas meter decreased: "
                f"last={last.value:.3f}, new={value:.3f}"
            )

    observations_file.parent.mkdir(parents=True, exist_ok=True)
    exists = observations_file.exists()

    with observations_file.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.writer(f)

        if not exists:
            writer.writerow(
                [
                    "timestamp_jst",
                    "meter_value_m3",
                    "source",
                ]
            )

        writer.writerow(
            [
                ts.isoformat(),
                f"{value:.6f}",
                "manual",
            ]
        )

    return True


# ============================================================
# 按分
# ============================================================

def interpolate_between(
    start: MeterPoint,
    end: MeterPoint,
    observations: Iterable[MeterPoint] = (),
) -> list[MeterPoint]:
    """
    start/end間を正時ごとに線形補間する。

    start/endや途中観測点は正時でなくてもよい。
    出力は原則として正時だけ。
    """
    require_finite(start.value)
    require_finite(end.value)
    observations = list(observations)
    validate_observations(observations)
    if start.ts >= end.ts:
        raise ValueError(
            "start timestamp must be before end timestamp"
        )

    if end.value < start.value:
        raise ValueError(
            "cumulative gas meter must not decrease"
        )

    anchors = [start]

    anchors.extend(
        point
        for point in observations
        if start.ts < point.ts < end.ts
    )

    anchors.append(end)
    anchors.sort(key=lambda point: point.ts)

    for left, right in zip(anchors, anchors[1:]):
        if left.ts == right.ts:
            raise ValueError(
                f"duplicate observation timestamp: {left.ts.isoformat()}"
            )

        if right.value < left.value:
            raise ValueError(
                "meter value decreases between anchors: "
                f"{left.ts.isoformat()}={left.value} -> "
                f"{right.ts.isoformat()}={right.value}"
            )

    def lerp(
        left: MeterPoint,
        right: MeterPoint,
        ts: datetime,
    ) -> float:
        total_seconds = (
            right.ts - left.ts
        ).total_seconds()

        elapsed_seconds = (
            ts - left.ts
        ).total_seconds()

        ratio = elapsed_seconds / total_seconds

        return (
            left.value
            + (right.value - left.value) * ratio
        )

    result: list[MeterPoint] = []

    cursor = start.ts

    if not is_exact_hour(cursor):
        cursor = (
            cursor.replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            + timedelta(hours=1)
        )

    if is_exact_hour(start.ts):
        result.append(
            MeterPoint(
                ts=start.ts,
                value=start.value,
                source=start.source,
            )
        )
        # start 自体をすでに追加したので、while では次の正時から処理する。
        cursor += timedelta(hours=1)

    anchor_index = 0

    while cursor < end.ts:
        while (
            anchor_index + 1 < len(anchors) - 1
            and cursor > anchors[anchor_index + 1].ts
        ):
            anchor_index += 1

        left = anchors[anchor_index]
        right = anchors[anchor_index + 1]

        result.append(
            MeterPoint(
                ts=cursor,
                value=lerp(left, right, cursor),
                source="interpolated",
            )
        )

        cursor += timedelta(hours=1)

    # CSV境界など、終了が正時なら終端も含める。
    if is_exact_hour(end.ts):
        if not result or result[-1].ts != end.ts:
            result.append(
                MeterPoint(
                    ts=end.ts,
                    value=end.value,
                    source=end.source,
                )
            )

    return result


def build_period_series(
    period: BillingPeriod,
    start_value: float,
    manual_observations: Sequence[MeterPoint],
) -> list[MeterPoint]:

    start = MeterPoint(
        ts=period.start_ts,
        value=start_value,
        source=f"csv:{period.bill_month}:start",
    )

    end_value = start_value + period.usage_m3

    end = MeterPoint(
        ts=period.end_ts,
        value=end_value,
        source=f"csv:{period.bill_month}:end",
    )

    for point in manual_observations:
        for boundary in (start, end):
            if point.ts == boundary.ts and not math.isclose(point.value, boundary.value, rel_tol=0, abs_tol=1e-9):
                raise ValueError("manual observation conflicts with CSV boundary")

    inside = [
        point
        for point in manual_observations
        if period.start_ts < point.ts < period.end_ts
    ]

    for point in inside:
        if not (
            start_value
            <= point.value
            <= end_value
        ):
            raise ValueError(
                "manual observation outside CSV-constrained range: "
                f"{point.ts.isoformat()}={point.value:.3f}, "
                f"expected {start_value:.3f}..{end_value:.3f} "
                f"for {period.bill_month}"
            )

    return interpolate_between(
        start,
        end,
        inside,
    )


def build_csv_series(
    periods: Sequence[BillingPeriod],
    manual_observations: Sequence[MeterPoint],
    anchor_ts: datetime,
    anchor_value: float,
) -> list[MeterPoint]:

    require_finite(anchor_value)
    validate_observations(manual_observations)
    current_ts = anchor_ts
    current_value = anchor_value
    all_points: list[MeterPoint] = []

    for period in periods:
        if period.end_ts <= anchor_ts:
            continue

        if period.start_ts != current_ts:
            raise ValueError(
                "billing history is not contiguous with anchor:\n"
                f"  current anchor : "
                f"{current_ts.isoformat()} = {current_value:.3f} m3\n"
                f"  next CSV start: "
                f"{period.start_ts.isoformat()} ({period.bill_month})"
            )

        segment = build_period_series(
            period,
            current_value,
            manual_observations,
        )

        if (
            all_points
            and segment
            and all_points[-1].ts == segment[0].ts
        ):
            segment = segment[1:]

        all_points.extend(segment)

        current_ts = period.end_ts
        current_value += period.usage_m3

    return all_points


def extend_series_with_manual_tail(
    points: Sequence[MeterPoint],
    manual_observations: Sequence[MeterPoint],
) -> tuple[list[MeterPoint], MeterPoint | None]:
    """
    最新CSV終端より後にある手動観測を使い、現在までの系列を延長する。

    戻り値:
      merged_points
      latest_exact_observation
    """
    validate_observations(manual_observations)
    if not points:
        return list(points), None

    merged = list(points)

    # CSVモードで作った系列の終端は、最後のCSV確定点。
    base = merged[-1]

    tail_observations = [
        point
        for point in manual_observations
        if point.ts > base.ts
    ]

    if not tail_observations:
        return merged, None

    current_anchor = base

    for observation in tail_observations:
        if observation.value < current_anchor.value:
            raise ValueError(
                "manual observation is below latest known meter value: "
                f"{observation.ts.isoformat()}={observation.value:.3f}, "
                f"latest={current_anchor.value:.3f}"
            )

        segment = interpolate_between(
            current_anchor,
            observation,
        )

        # 先頭の正時が既存系列の末尾と重複する場合は除外。
        if (
            merged
            and segment
            and merged[-1].ts == segment[0].ts
        ):
            segment = segment[1:]

        merged.extend(segment)

        # 次区間は「実測時刻・実測値」から開始する。
        current_anchor = observation

    return merged, tail_observations[-1]


# ============================================================
# Preview CSV
# ============================================================

def write_hourly_csv(
    points: Sequence[MeterPoint],
    path: Path,
    base_value: float,
) -> None:

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.writer(f)

        writer.writerow(
            [
                "timestamp_jst",
                "state_m3",
                "sum_m3",
                "source",
            ]
        )

        for point in points:
            writer.writerow(
                [
                    point.ts.isoformat(),
                    f"{point.value:.6f}",
                    f"{point.value - base_value:.6f}",
                    point.source,
                ]
            )


def load_preview_points(path: Path) -> list[MeterPoint]:
    if not path.exists():
        return []

    points: list[MeterPoint] = []

    with path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        for row in reader:
            points.append(
                MeterPoint(
                    ts=parse_datetime_jst(row["timestamp_jst"]),
                    value=float(row["state_m3"]),
                    source=row.get("source") or "preview",
                )
            )

    points.sort(key=lambda point: point.ts)
    return points


def find_latest_csv_endpoint(
    points: Sequence[MeterPoint],
) -> MeterPoint | None:

    endpoints = [
        point
        for point in points
        if point.source.startswith("csv:")
        and point.source.endswith(":end")
    ]

    if not endpoints:
        return None

    return max(endpoints, key=lambda point: point.ts)


def rebuild_manual_tail_from_preview(
    preview_points: Sequence[MeterPoint],
    manual_observations: Sequence[MeterPoint],
) -> tuple[list[MeterPoint], MeterPoint]:

    csv_endpoint = find_latest_csv_endpoint(preview_points)

    if csv_endpoint is None:
        raise RuntimeError(
            "No CSV endpoint was found in the preview file. "
            "Run CSV mode once before recording manual observations."
        )

    # CSV終端より後の既存previewは捨てて、手動観測から毎回再計算。
    base_points = [
        point
        for point in preview_points
        if point.ts <= csv_endpoint.ts
    ]

    rebuilt, latest_observation = extend_series_with_manual_tail(
        base_points,
        manual_observations,
    )

    if latest_observation is None:
        raise RuntimeError(
            "No manual observation exists after the latest CSV endpoint."
        )

    return rebuilt, latest_observation



def validate_hourly_points(
    points: Sequence[MeterPoint],
) -> list[MeterPoint]:
    """
    PostgreSQLへ書く前に、正時系列の重複・並び・欠落を検査する。
    JSTにはDSTがないため、連続区間は3600秒刻みであることを期待する。
    """
    hourly = [
        point
        for point in points
        if is_exact_hour(point.ts)
    ]

    if not hourly:
        raise ValueError("no hourly points")

    hourly = sorted(hourly, key=lambda point: point.ts)

    seen: set[datetime] = set()

    for point in hourly:
        require_finite(point.value)
        if point.ts > datetime.now(JST):
            raise ValueError("future statistics are not supported")
        if point.ts in seen:
            raise ValueError(
                f"duplicate hourly timestamp detected: {point.ts.isoformat()}"
            )
        seen.add(point.ts)

    for left, right in zip(hourly, hourly[1:]):
        diff = (right.ts - left.ts).total_seconds()

        if diff != 3600:
            raise ValueError(
                "hourly series is not contiguous: "
                f"{left.ts.isoformat()} -> {right.ts.isoformat()} "
                f"({diff} seconds)"
            )

        if right.value < left.value:
            raise ValueError(
                "meter state decreased in hourly series: "
                f"{left.ts.isoformat()}={left.value:.6f} -> "
                f"{right.ts.isoformat()}={right.value:.6f}"
            )

    return hourly


# ============================================================
# PostgreSQL
# ============================================================

def import_psycopg2():
    try:
        import psycopg2
        return psycopg2
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 is required for --commit.\n"
            "Install with:\n"
            "  apt install python3-psycopg2\n"
            "or:\n"
            "  pip install psycopg2-binary"
        ) from exc


def connect_postgres():
    psycopg2 = import_psycopg2()

    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "homeassistant"),
        user=os.environ.get("PGUSER", "homeassistant"),
        password=os.environ.get("PGPASSWORD"),
    )


def validate_statistic_id(statistic_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_]+", statistic_id):
        raise ValueError("statistic_id must be external, e.g. gas_estimator:usage")
    source = statistic_id.split(":", 1)[0]
    if source == "recorder":
        raise ValueError("recorder is reserved for entity statistics")
    return source


def get_metadata_id(conn, statistic_id: str) -> int:
    source = validate_statistic_id(statistic_id)
    expected = (source, "m³", False, True, 0, "volume")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO statistics_meta
                (statistic_id, source, unit_of_measurement, has_mean,
                 has_sum, name, mean_type, unit_class)
            VALUES (%s, %s, %s, false, true, %s, 0, 'volume')
            ON CONFLICT (statistic_id) DO NOTHING
            """,
            (statistic_id, source, "m³", "Gas Usage Estimated"),
        )
        cur.execute(
            """
            SELECT id, source, unit_of_measurement, has_mean, has_sum,
                   mean_type, unit_class
            FROM statistics_meta WHERE statistic_id = %s FOR UPDATE
            """,
            (statistic_id,),
        )
        row = cur.fetchone()
    if row is None or tuple(row[1:]) != expected:
        raise RuntimeError(f"incompatible external statistics metadata: {statistic_id}")
    return int(row[0])


def upsert_statistics(
    conn,
    metadata_id: int,
    points: Sequence[MeterPoint],
    base_value: float,
) -> int:

    hourly_points = validate_hourly_points(points)

    now_ts = datetime.now(tz=JST).timestamp()

    rows = [
        (
            now_ts,
            metadata_id,
            point.ts.timestamp(),
            point.value,
            point.value - base_value,
        )
        for point in hourly_points
    ]

    sql = """
        INSERT INTO statistics (
            created_ts,
            metadata_id,
            start_ts,
            state,
            sum
        )
        VALUES (%s, %s, %s, %s, %s)

        ON CONFLICT (metadata_id, start_ts)

        DO UPDATE SET
            created_ts = EXCLUDED.created_ts,
            state      = EXCLUDED.state,
            sum        = EXCLUDED.sum
    """

    with conn.cursor() as cur:
        cur.executemany(sql, rows)

    return len(rows)


def verify_statistics(conn, metadata_id, points, base_value) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT start_ts, state, sum FROM statistics
            WHERE metadata_id = %s AND start_ts >= %s AND start_ts <= %s
            ORDER BY start_ts
            """,
            (metadata_id, points[0].ts.timestamp(), points[-1].ts.timestamp()),
        )
        rows = cur.fetchall()
    if len(rows) != len(points):
        raise RuntimeError(f"verification failed: expected {len(points)} rows, got {len(rows)}")
    for row, point in zip(rows, points):
        expected = (point.ts.timestamp(), point.value, point.value - base_value)
        if any(actual is None or not math.isclose(float(actual), wanted, rel_tol=0, abs_tol=1e-7)
               for actual, wanted in zip(row, expected)):
            raise RuntimeError(f"verification failed at {point.ts.isoformat()}")


def commit_statistics(
    points: Sequence[MeterPoint],
    base_value: float,
    statistic_id: str,
) -> None:

    validate_statistic_id(statistic_id)
    require_finite(base_value)
    hourly_points = validate_hourly_points(points)
    if hourly_points[0].value < base_value:
        raise ValueError("meter state is below sum baseline")

    conn = connect_postgres()

    try:
        conn.autocommit = False

        metadata_id = get_metadata_id(
            conn,
            statistic_id,
        )

        print()
        print("PostgreSQL write")
        print(f"  statistic_id: {statistic_id}")
        print(f"  metadata_id : {metadata_id}")
        print(f"  rows        : {len(hourly_points)}")
        print(f"  from        : {hourly_points[0].ts.isoformat()}")
        print(f"  to          : {hourly_points[-1].ts.isoformat()}")

        written = upsert_statistics(
            conn,
            metadata_id,
            hourly_points,
            base_value,
        )

        verify_statistics(conn, metadata_id, hourly_points, base_value)

        conn.commit()

        print()
        print(f"COMMIT complete: {written} rows")

    except Exception:
        conn.rollback()
        print("ROLLBACK", file=sys.stderr)
        raise

    finally:
        conn.close()


# ============================================================
# CLI modes
# ============================================================

def record_mode(
    args: argparse.Namespace,
) -> int:

    value = float(args.target)
    require_finite(value)

    if args.at:
        ts = parse_datetime_jst(args.at)
    else:
        ts = datetime.now(JST)

    preview_points = load_preview_points(
        args.output
    )

    if not preview_points:
        raise RuntimeError(
            f"Preview file does not exist or is empty: {args.output}\n"
            "Run CSV mode first, e.g.:\n"
            "  python3 gas_usage_interpolator.py gas/0904.csv"
        )

    manual = load_manual_observations(
        args.observations_file
    )

    endpoint = find_latest_csv_endpoint(preview_points)
    if endpoint is None or ts <= endpoint.ts:
        raise ValueError("manual mode requires a timestamp after the latest CSV endpoint")
    existing = [p for p in manual if p.ts == ts]
    if existing and any(abs(p.value - value) >= 1e-9 for p in existing):
        raise ValueError("manual observation already exists with different value")
    if not existing:
        if manual and ts < manual[-1].ts:
            raise ValueError("new observation is older than the latest manual observation")
        manual.append(MeterPoint(ts, value, "manual"))
    validate_observations(manual)

    rebuilt, latest_observation = rebuild_manual_tail_from_preview(
        preview_points,
        manual,
    )

    validate_hourly_points(rebuilt)
    saved = save_manual_observation(value, ts, args.observations_file)
    print("Manual gas-meter observation " + ("recorded" if saved else "already recorded"))
    print(f"  time : {ts.isoformat()}")
    print(f"  value: {value:.6f} m3")

    write_hourly_csv(
        rebuilt,
        args.output,
        args.anchor_value,
    )

    csv_endpoint = find_latest_csv_endpoint(
        rebuilt
    )

    if csv_endpoint is None:
        raise RuntimeError(
            "CSV endpoint disappeared from preview unexpectedly."
        )

    print()
    print("Manual tail interpolation")
    print(
        f"  from : {csv_endpoint.ts.isoformat()}  "
        f"{csv_endpoint.value:.3f} m3"
    )
    print(
        f"  to   : {latest_observation.ts.isoformat()}  "
        f"{latest_observation.value:.3f} m3"
    )
    print(
        f"  delta: "
        f"{latest_observation.value - csv_endpoint.value:.3f} m3"
    )
    print(
        f"  preview end hourly point: "
        f"{rebuilt[-1].ts.isoformat()}  "
        f"{rebuilt[-1].value:.3f} m3"
    )
    print(f"  output: {args.output}")

    if not args.commit:
        print()
        print(
            "Dry run only. PostgreSQL and Home Assistant "
            "were NOT modified."
        )
        print(
            "Use --commit to write the interpolated tail "
            "to external statistics."
        )
        return 0

    # DBはCSV終端以降だけUPSERTすれば十分。
    tail_points = [
        point
        for point in rebuilt
        if point.ts >= csv_endpoint.ts
    ]

    commit_statistics(
        tail_points,
        args.anchor_value,
        args.statistic_id,
    )

    return 0


def csv_mode(
    args: argparse.Namespace,
) -> int:

    csv_path = Path(args.target)

    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    anchor_ts = parse_datetime_jst(
        args.anchor_time
    )

    anchor_value = args.anchor_value

    periods, skipped, encoding = load_osakagas_csv(
        csv_path
    )

    manual = load_manual_observations(
        args.observations_file
    )

    print(f"CSV encoding: {encoding}")
    print(
        f"Anchor: "
        f"{anchor_ts.isoformat()} = "
        f"{anchor_value:.3f} m3"
    )
    print(
        f"Manual observations loaded: {len(manual)}"
    )

    points = build_csv_series(
        periods,
        manual,
        anchor_ts,
        anchor_value,
    )

    if skipped:
        print()
        print("Special/skipped CSV rows:")

        for row in skipped:
            print(
                f"  {row.get('ご請求月')}: "
                f"period={row.get('ガス使用期間')!r}, "
                f"usage={row.get('ガス使用量(m3)')!r}"
            )

    if not points:
        print()
        print(
            "No contiguous hourly series was generated."
        )
        return 2

    # 最新CSV終端より後に手動観測が既に存在する場合は、
    # そこまでpreviewを延長する。
    points, latest_tail_observation = extend_series_with_manual_tail(
        points,
        manual,
    )

    validate_hourly_points(points)
    write_hourly_csv(
        points,
        args.output,
        anchor_value,
    )

    print()
    print(f"Generated {len(points)} hourly points")
    print(
        f"  start: "
        f"{points[0].ts.isoformat()}  "
        f"{points[0].value:.3f} m3"
    )
    print(
        f"  end  : "
        f"{points[-1].ts.isoformat()}  "
        f"{points[-1].value:.3f} m3"
    )
    print(
        f"  sum at preview end: "
        f"{points[-1].value - anchor_value:.3f} m3"
    )
    print(f"  output: {args.output}")

    if latest_tail_observation is not None:
        print(
            f"  latest manual observation: "
            f"{latest_tail_observation.ts.isoformat()}  "
            f"{latest_tail_observation.value:.3f} m3"
        )

    if not args.commit:
        print()
        print(
            "Dry run only. PostgreSQL and Home Assistant "
            "were NOT modified."
        )
        print(
            "Use --commit to write statistics "
            "to external statistics."
        )
        return 0

    commit_statistics(
        points,
        anchor_value,
        args.statistic_id,
    )

    return 0


# ============================================================
# main
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "CSV path = process Osaka Gas CSV; "
            "numeric argument = record a manual gas-meter observation."
        )
    )

    parser.add_argument(
        "target",
        help=(
            "Osaka Gas CSV path, or current "
            "meter value such as 459.439"
        ),
    )

    parser.add_argument(
        "--at",
        help=(
            "manual observation timestamp; "
            "default is now (JST)"
        ),
    )

    parser.add_argument(
        "--anchor-time",
        default=DEFAULT_ANCHOR_TIME,
        help=(
            "starting meter timestamp "
            f"(default: {DEFAULT_ANCHOR_TIME})"
        ),
    )

    parser.add_argument(
        "--anchor-value",
        type=float,
        default=DEFAULT_ANCHOR_VALUE,
        help=(
            "starting cumulative meter value "
            f"(default: {DEFAULT_ANCHOR_VALUE})"
        ),
    )

    parser.add_argument(
        "--observations-file",
        type=Path,
        default=DEFAULT_OBSERVATIONS_FILE,
        help=(
            "manual observation file "
            f"(default: {DEFAULT_OBSERVATIONS_FILE})"
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=(
            "preview output CSV "
            f"(default: {DEFAULT_OUTPUT_FILE})"
        ),
    )

    parser.add_argument(
        "--statistic-id",
        default=DEFAULT_STATISTIC_ID,
        help=(
            "Home Assistant statistic_id "
            f"(default: {DEFAULT_STATISTIC_ID})"
        ),
    )

    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "write generated hourly statistics to PostgreSQL "
            "for an external statistic"
        ),
    )

    args = parser.parse_args()
    validate_statistic_id(args.statistic_id)
    require_finite(args.anchor_value)

    if looks_like_number(args.target):
        return record_mode(args)

    return csv_mode(args)


if __name__ == "__main__":
    try:
        sys.exit(main())

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
