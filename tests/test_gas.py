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


if __name__ == '__main__':
    unittest.main()
