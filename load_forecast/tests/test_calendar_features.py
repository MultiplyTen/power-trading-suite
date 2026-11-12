"""Calendar semantics and causal load-feature regression tests."""
import copy
from datetime import timedelta
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configuration import read_config, validate
from features.calendar import calendar
from features.load import baseline, make_features
from weather.features import VARS


ROOT = Path(__file__).resolve().parents[1]


class CalendarFeatures(unittest.TestCase):
    def setUp(self):
        self.config = read_config(ROOT / 'config.yaml')

    def test_six_soft_borrow_preserves_constant_without_same_type(self):
        # First statutory holiday in this lookback has no matching holiday,
        # so six must borrow ordinary weekends, without halving their level.
        index = pd.date_range('2026-08-01', '2026-09-25 23:00', freq='h')
        config = copy.deepcopy(self.config)
        config['training']['day_type_granularity'] = 'six'
        for value in (0., 100.):
            series = pd.Series(value, index=index)
            prediction = baseline(series, config).loc['2026-09-25']
            np.testing.assert_allclose(prediction, value)

    def test_six_sparse_matching_and_borrowed_samples_preserve_constant(self):
        index = pd.date_range('2026-08-01', '2026-09-24 23:00', freq='h')
        config = copy.deepcopy(self.config)
        config['training'].update(day_type_granularity='six', day_type_min_weight=100.)
        # Force mixed same-type/borrowed evidence: a constant must remain a
        # constant regardless of confidence weight and presence of exact days.
        for soft in (.1, .5, 1.):
            config['training']['day_type_soft_weight'] = soft
            result = baseline(pd.Series(100., index=index), config)
            np.testing.assert_allclose(result.loc['2026-09-01':], 100.)

    def test_enhanced_weekday_schema_and_makeup_boundary(self):
        index = pd.DatetimeIndex(['2026-09-20 06:00', '2026-10-09 06:00',
                                  '2026-10-10 06:00', '2026-10-11 06:00'])
        features = calendar(index, 'enhanced')
        # Sunday 9/20 and Saturday 10/10 are makeup workdays, not holidays.
        self.assertEqual(features.loc[index[0], 'weekday_sun'], 1.)
        self.assertEqual(features.loc[index[0], 'makeup_workday'], 1.)
        self.assertEqual(features.loc[index[2], 'weekday_sat'], 1.)
        self.assertEqual(features.loc[index[2], 'holiday'], 0.)
        self.assertEqual(features.loc[index[2], 'workday'], 1.)
        self.assertEqual(features.loc[index[2], 'weekday_sat_hour_sin'], 1.)
        # Named makeup Saturday must not create a holiday on Sunday or a
        # pre-holiday indicator on Friday. An ordinary Sunday remains distinct.
        self.assertEqual(features.loc[index[1], 'pre_holiday'], 0.)
        self.assertEqual(features.loc[index[3], 'holiday'], 0.)
        self.assertEqual(features.loc[index[3], 'post_holiday'], 0.)
        self.assertEqual(features.loc[index[3], 'holiday_day_index'], 0.)
        weekday_cols = ['weekday_' + day for day in ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')]
        np.testing.assert_allclose(features[weekday_cols].sum(axis=1), 1.)

    def test_statutory_sequence_uses_dates_beyond_requested_slice(self):
        # A one-hour slice from the middle still finds the whole public holiday.
        mid = calendar(pd.DatetimeIndex(['2026-10-03 12:00']), 'enhanced').iloc[0]
        self.assertEqual(mid.holiday_day_index, 3.)
        self.assertEqual(mid.holiday_days_remaining, 4.)
        index = pd.date_range('2026-10-07', '2026-10-12', freq='D')
        features = calendar(index, 'enhanced')
        self.assertEqual(features.loc['2026-10-07', 'holiday_day_index'], 7.)
        self.assertEqual(features.loc['2026-10-07', 'holiday_days_remaining'], 0.)
        for offset, date in enumerate(('2026-10-08', '2026-10-09', '2026-10-10'), 1):
            self.assertEqual(features.loc[date, f'post_holiday_day{offset}'], 1.)
        self.assertEqual(features.loc['2026-10-11', ['post_holiday_day1', 'post_holiday_day2', 'post_holiday_day3']].sum(), 0.)

    def test_basic_mode_keeps_historical_schema_and_optional_call(self):
        index = pd.date_range('2026-10-01', periods=48, freq='h')
        old = calendar(index)
        basic = calendar(index, 'basic')
        pd.testing.assert_frame_equal(old, basic)
        self.assertNotIn('weekday_mon', basic)
        self.assertIn('weekday_mon', calendar(index, 'enhanced'))
        with self.assertRaisesRegex(ValueError, 'calendar_features'):
            calendar(index, 'invalid')

    def test_future_load_does_not_change_enhanced_forecast_features(self):
        index = pd.date_range('2026-05-01', '2026-10-15 23:00', freq='h')
        series = pd.Series(100 + np.sin(np.arange(len(index)) / 24), index=index)
        weather = pd.DataFrame(1., index=index, columns=VARS)
        weather['temperature_2m'] = 28.
        target = pd.Timestamp('2026-10-10')
        last_visible = target - timedelta(days=self.config['run']['observation_lag_days']) + timedelta(hours=23)
        changed = series.copy()
        changed.loc[changed.index > last_visible] = 1e8
        first, x_first = make_features(series, weather, self.config, 26)
        second, x_second = make_features(changed, weather, self.config, 26)
        target_hours = pd.date_range(target, periods=24, freq='h')
        pd.testing.assert_frame_equal(x_first.loc[target_hours], x_second.loc[target_hours])
        pd.testing.assert_frame_equal(first.loc[target_hours, ['baseline', 'scale']],
                                      second.loc[target_hours, ['baseline', 'scale']])
        # Labels legitimately change; they must not be mistaken for features.
        self.assertFalse(first.loc[target_hours, 'actual'].equals(second.loc[target_hours, 'actual']))

    def test_calendar_mode_config_validation(self):
        self.assertEqual(self.config['training']['day_type_granularity'], 'binary')
        self.assertEqual(self.config['training']['calendar_features'], 'enhanced')
        invalid = copy.deepcopy(self.config)
        invalid['training']['calendar_features'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'calendar_features'):
            validate(invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
