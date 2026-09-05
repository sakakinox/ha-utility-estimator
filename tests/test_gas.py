import tempfile
import unittest
from datetime import date
from pathlib import Path

import gas_usage_interpolator as gas


def point(ts, value):
    return gas.MeterPoint(gas.parse_datetime_jst(ts), value, 'manual')


def periods():
    return [gas.BillingPeriod('202607', date(2026, 6, 25), date(2026, 7, 24), 25),
            gas.BillingPeriod('202608', date(2026, 7, 25), date(2026, 8, 25), 16)]


class GasTests(unittest.TestCase):
    def series(self, manual=()):
        return gas.build_csv_series(periods(), manual, gas.parse_datetime_jst(gas.DEFAULT_ANCHOR_TIME), 412)

    def test_reference_series(self):
        manual = [point('2026-09-04T13:53:57', 459.439)]
        result, latest = gas.extend_series_with_manual_tail(self.series(), manual)
        self.assertEqual(len(result), 1706)
        self.assertEqual(result[0].value, 412)
        self.assertEqual(result[-1].ts, gas.parse_datetime_jst('2026-09-04T13:00'))
        self.assertAlmostEqual(result[-1].value, 459.412429, places=6)
        self.assertEqual(latest, manual[0])
        self.assertEqual(len(gas.validate_hourly_points(result)), 1706)

    def test_csv_boundary(self):
        result = self.series()
        self.assertEqual(len({p.ts for p in result}), len(result))
        self.assertEqual(result[-1].value, 453)
        self.assertEqual(periods()[1].end_ts, gas.parse_datetime_jst('2026-08-26T12:00'))

    def test_future_csv_rebuild(self):
        manual = [point('2026-07-10T13:45', 425)]
        result = self.series(manual)
        self.assertEqual(result[-1].value, 453)
        self.assertNotEqual(result[360].value, self.series()[360].value)

    def test_conflicting_internal_observation(self):
        with self.assertRaises(ValueError):
            self.series([point('2026-07-10T12:00', 440)])

    def test_duplicate_and_empty_hours(self):
        p = point('2026-07-10T12:00', 425)
        for values in ([], [p, p]):
            with self.assertRaises(ValueError):
                gas.validate_hourly_points(values)

    def test_empty_periods(self):
        self.assertEqual(gas.build_csv_series([], [], gas.parse_datetime_jst(gas.DEFAULT_ANCHOR_TIME), 412), [])

    def test_observation_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manual.csv'
            ts = gas.parse_datetime_jst('2026-09-04T13:53:57')
            self.assertTrue(gas.save_manual_observation(459.439, ts, path))
            self.assertFalse(gas.save_manual_observation(459.439, ts, path))
            with self.assertRaises(ValueError):
                gas.save_manual_observation(460, ts, path)

    def test_csv_skip_and_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'gas.csv'
            path.write_bytes(('説明\nご請求月,ガス使用期間,ガス使用量(m3),ガス使用日数(日)\n'
                              '202606,-,0,-\n202607,2026年6月25日～2026年7月24日,25,30\n').encode('cp932'))
            result, skipped, encoding = gas.load_osakagas_csv(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(len(skipped), 1)
            self.assertEqual(encoding, 'cp932')


class SafetyTests(unittest.TestCase):
    def test_boundary_conflicts(self):
        for ts, value in [('2026-06-25T12:00', 413), ('2026-07-25T12:00', 438), ('2026-08-26T12:00', 452)]:
            with self.subTest(ts=ts), self.assertRaises(ValueError):
                gas.build_csv_series(periods(), [point(ts, value)], gas.parse_datetime_jst(gas.DEFAULT_ANCHOR_TIME), 412)

    def test_invalid_periods(self):
        for history in [periods()[::-1], periods() + [periods()[1]]]:
            with self.assertRaises(ValueError):
                gas.build_csv_series(history, [], gas.parse_datetime_jst(gas.DEFAULT_ANCHOR_TIME), 412)

    def test_invalid_values_and_future(self):
        for p in [point('2026-09-04T13:00', float('nan')), point('2026-09-04T13:00', float('inf')), point('2999-01-01T00:00', 500)]:
            with self.assertRaises(ValueError):
                gas.validate_hourly_points([p])

    def test_multiple_non_hourly_anchors(self):
        base = [point('2026-09-01T12:00', 453)]
        manual = [point('2026-09-01T12:15', 454), point('2026-09-01T12:45', 455), point('2026-09-01T13:15', 457)]
        result, latest = gas.extend_series_with_manual_tail(base, manual)
        self.assertEqual([p.value for p in result], [453, 456])
        self.assertEqual(latest, manual[-1])

    def test_manual_error_does_not_save(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as folder:
            preview = Path(folder) / 'preview.csv'
            observations = Path(folder) / 'manual.csv'
            series = gas.build_csv_series(periods(), [], gas.parse_datetime_jst(gas.DEFAULT_ANCHOR_TIME), 412)
            gas.write_hourly_csv(series, preview, 412)
            before = preview.read_bytes()
            args = Namespace(target='450', at='2026-09-04T13:53:57', output=preview,
                             observations_file=observations, anchor_value=412, commit=False)
            with self.assertRaises(ValueError):
                gas.record_mode(args)
            self.assertFalse(observations.exists())
            self.assertEqual(preview.read_bytes(), before)

    def test_cli_error_exit(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, gas.__file__, 'missing.csv'], capture_output=True)
        self.assertEqual(result.returncode, 1)


class DatabaseTests(unittest.TestCase):
    """Mock the DB boundary; real PostgreSQL compatibility remains a separate check."""
    def connection(self):
        from unittest.mock import MagicMock
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (200, 'gas_estimator', 'm³', False, True, 0, 'volume')
        cur.fetchall.return_value = [(p.ts.timestamp(), p.value, p.value - 412) for p in self.points]
        return conn, cur

    def setUp(self):
        self.points = [point('2026-06-25T12:00', 412), point('2026-06-25T13:00', 413)]

    def test_transaction_and_replay(self):
        from unittest.mock import patch
        conn, cur = self.connection()
        with patch.object(gas, 'connect_postgres', return_value=conn):
            gas.commit_statistics(self.points, 412, gas.DEFAULT_STATISTIC_ID)
            first = cur.executemany.call_args.args
            gas.commit_statistics(self.points, 412, gas.DEFAULT_STATISTIC_ID)
            second = cur.executemany.call_args.args
        self.assertEqual([r[1:] for r in first[1]], [r[1:] for r in second[1]])
        self.assertIn('ON CONFLICT (metadata_id, start_ts)', first[0])
        self.assertEqual(conn.commit.call_count, 2)
        conn.rollback.assert_not_called()
        sql = ' '.join(call.args[0] for call in cur.execute.call_args_list)
        self.assertNotIn('statistics_short_term', sql)
        self.assertNotIn('sensor.', sql)
        self.assertIn('ON CONFLICT (statistic_id) DO NOTHING', sql)
        self.assertFalse(conn.autocommit)

    def test_verification_failure_rolls_back(self):
        from unittest.mock import patch
        for rows in [[], [(p.ts.timestamp(), p.value, p.value) for p in self.points]]:
            conn, cur = self.connection()
            cur.fetchall.return_value = rows
            with patch.object(gas, 'connect_postgres', return_value=conn), self.assertRaises(RuntimeError):
                gas.commit_statistics(self.points, 412, gas.DEFAULT_STATISTIC_ID)
            conn.commit.assert_not_called()
            conn.rollback.assert_called_once()
            conn.close.assert_called_once()

    def test_metadata_mismatch_rolls_back(self):
        from unittest.mock import patch
        conn, cur = self.connection()
        cur.fetchone.return_value = (192, 'recorder', 'm³', False, True, 0, 'volume')
        with patch.object(gas, 'connect_postgres', return_value=conn), self.assertRaises(RuntimeError):
            gas.commit_statistics(self.points, 412, gas.DEFAULT_STATISTIC_ID)
        conn.rollback.assert_called_once()
        cur.executemany.assert_not_called()

    def test_write_failure_rolls_back(self):
        from unittest.mock import patch
        conn, cur = self.connection()
        cur.executemany.side_effect = RuntimeError('write failed')
        with patch.object(gas, 'connect_postgres', return_value=conn), self.assertRaises(RuntimeError):
            gas.commit_statistics(self.points, 412, gas.DEFAULT_STATISTIC_ID)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_entity_id_rejected_before_connection(self):
        from unittest.mock import patch
        with patch.object(gas, 'connect_postgres') as connect:
            for statistic_id in ['sensor.gas_usage_estimated', 'recorder:usage']:
                with self.assertRaises(ValueError):
                    gas.commit_statistics(self.points, 412, statistic_id)
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
