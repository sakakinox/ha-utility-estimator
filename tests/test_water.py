import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import gas_usage_interpolator as gas
import utility_interpolator as cli
import water_usage_interpolator as water


def bill(start='2026-05-20', end='2026-07-03', previous=935, current=947, usage=12):
    return dict(start=start, end=end, previous=previous, current=current, usage=usage)


class WaterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.history = Path(self.folder.name) / 'history.json'
        self.preview = Path(self.folder.name) / 'preview.csv'
        self.paths = ['--history', str(self.history), '--output', str(self.preview)]
        self.reading = ['--start', '2026-05-20', '--end', '2026-07-03', '--previous', '935', '--current', '947', '--usage', '12']
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_reference(self):
        points, baseline = water.build_series([bill()])
        self.assertEqual(len(points), 1057)
        self.assertEqual(baseline, 935)
        self.assertEqual(points[0].value, 935)
        self.assertEqual(points[-1].value, 947)
        self.assertEqual(points[-1].value - baseline, 12)
        self.assertEqual(points[-1].ts, gas.parse_datetime_jst('2026-07-03T12:00'))

    def test_second_period_preserves_baseline_and_deduplicates(self):
        second = bill('2026-07-03', '2026-08-20', 947, 962, 15)
        points, baseline = water.build_series(water.add_bill([bill()], second))
        self.assertEqual(baseline, 935)
        self.assertEqual(points[-1].value - baseline, 27)
        self.assertEqual(len(points), len({p.ts for p in points}))

    def test_precise_times(self):
        points, baseline = water.build_series([bill('2026-05-20T12:30', '2026-05-20T14:30', 935, 947, 12)])
        self.assertEqual([p.value for p in points], [938, 944])
        self.assertEqual(baseline, 935)

    def test_zero_usage(self):
        points, baseline = water.build_series([bill(current=935, usage=0)])
        self.assertTrue(all(p.value == baseline for p in points))

    def test_invalid_intervals(self):
        invalid = [bill(usage=13), bill(current=934, usage=-1), bill(previous=float('nan')),
                   bill(usage=float('inf')), bill(end='2026-05-20'), bill(end='2026-05-19'),
                   bill(end='2999-01-01'), bill(previous=-1, current=11)]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                water.build_series([item])

    def test_gaps_overlaps_boundary_mismatch_and_prepend(self):
        for item in [bill('2026-07-04', '2026-08-20', 947, 959),
                     bill('2026-07-02', '2026-08-20', 947, 959),
                     bill('2026-07-03', '2026-08-20', 948, 960),
                     bill('2026-04-01', '2026-05-20', 923, 935)]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                water.add_bill([bill()], item)

    def test_cli_replay_and_rebuild(self):
        with patch.object(gas, 'connect_postgres') as connect:
            self.assertEqual(cli.main(['water', *self.paths, *self.reading]), 0)
            first = (self.history.read_bytes(), self.preview.read_bytes())
            cli.main(['water', *self.paths, *self.reading])
            cli.main(['water', *self.paths])
            self.assertEqual(first, (self.history.read_bytes(), self.preview.read_bytes()))
            connect.assert_not_called()
        self.assertEqual(len(json.loads(self.history.read_text())['bills']), 1)

    def test_failure_preserves_files(self):
        water.main([*self.paths, *self.reading])
        before = (self.history.read_bytes(), self.preview.read_bytes())
        with self.assertRaises(ValueError):
            water.main([*self.paths, *self.reading, '--current', '948', '--usage', '13'])
        self.assertEqual(before, (self.history.read_bytes(), self.preview.read_bytes()))

    def test_invalid_first_input_does_not_save(self):
        with self.assertRaises(ValueError):
            water.main([*self.paths, *self.reading, '--usage', '13'])
        self.assertFalse(self.history.exists())
        self.assertFalse(self.preview.exists())

    def test_empty_and_partial_input(self):
        with self.assertRaises(ValueError):
            water.main(self.paths)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            water.main([*self.paths, '--start', '2026-05-20'])
        self.assertEqual(error.exception.code, 2)

    def test_statistic_and_path_separation(self):
        for args in [['--statistic-id', 'gas_estimator:usage'], ['--output', str(self.history)]]:
            with self.assertRaises(ValueError):
                water.main([*self.paths, *self.reading, *args])
        water.main([*self.paths, *self.reading])
        with self.assertRaises(ValueError):
            water.main([*self.paths, '--statistic-id', 'water_estimator:other'])

    def test_water_commit_metadata_and_values(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (201, 'water_estimator', 'm³', False, True, 0, 'volume')
        points, baseline = water.build_series([bill()])
        cur.fetchall.return_value = [(p.ts.timestamp(), p.value, p.value - baseline) for p in points]
        with patch.object(gas, 'connect_postgres', return_value=conn):
            water.main([*self.paths, *self.reading, '--commit'])
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        self.assertEqual(cur.execute.call_args_list[0].args[1], ('water_estimator:usage', 'water_estimator', 'm³', 'Water Usage Estimated'))
        rows = cur.executemany.call_args.args[1]
        self.assertEqual(rows[0][-2:], (935, 0))
        self.assertEqual(rows[-1][-2:], (947, 12))

    def test_water_commit_failure_keeps_retryable_history(self):
        with patch.object(gas, 'commit_statistics', side_effect=RuntimeError('DB failure')):
            with self.assertRaises(RuntimeError):
                water.main([*self.paths, *self.reading, '--commit'])
        with patch.object(gas, 'commit_statistics') as commit:
            water.main([*self.paths, '--commit'])
            self.assertEqual(commit.call_args.args[1:], (935, 'water_estimator:usage'))

    def test_gas_dispatch(self):
        with patch.object(gas, 'main', return_value=0) as main:
            self.assertEqual(cli.main(['gas', 'billing.csv']), 0)
            main.assert_called_once_with(['billing.csv'])


if __name__ == '__main__':
    unittest.main()
