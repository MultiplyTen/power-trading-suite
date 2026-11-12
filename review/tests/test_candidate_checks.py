import unittest
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from candidate_checks import (daily_improvement, passes_improvement, risk_validation_guard,
                              risk_evidence_passed, empirical_cvar90)
from suggestions import day_bootstrap_ci
from apply_config import validate_value


class CandidateChecksTests(unittest.TestCase):
    def hourly(self, **values):
        return pd.DataFrame(values, index=pd.date_range('2026-08-01', periods=48, freq='h'))

    def risk_frame(self, profits, **values):
        frame = pd.DataFrame({'profit': profits, 'feasible': True, 'limit': 20.0,
                              'period_hours': 24, 'scenario_days': 90,
                              'reference_limit': 20., 'reference_confidence': .9,
                              'reference_error_shrink': 1., 'reference_window_days': 90,
                              'reference_scenario_days': 90, 'reference_scenario_hash': 'a' * 64,
                              'reference_config_hash': 'c' * 64,
                              'reference_weighting': 'equal_proxy_scenarios',
                              'reference_baseline_sha256': 'b' * 64,
                              'reference_scenario_exceed': 0., 'reference_scenario_feasible': True,
                              'reference_scenario_cvar90_cny': 10.},
                             index=pd.date_range('2026-08-01', periods=len(profits), freq='D'))
        frame.attrs['artifact_sha256'] = 'b' * 64
        for key, value in values.items():
            frame[key] = value
        return frame

    def test_improvement_sign_for_losses_and_profit(self):
        load_base = self.hourly(actual=10.0, prediction=12.0)
        load_new = self.hourly(actual=10.0, prediction=11.0)
        self.assertTrue((daily_improvement(load_base, load_new, 'load') > 0).all())
        self.assertTrue((daily_improvement(load_new, load_base, 'load') < 0).all())
        price_base = self.hourly(da=100., rt=120., da_hat=110., rt_hat=130.)
        price_new = self.hourly(da=100., rt=120., da_hat=102., rt_hat=122.)
        self.assertTrue((daily_improvement(price_base, price_new, 'price') > 0).all())
        risk_base = self.risk_frame([-10.] * 12)
        risk_new = self.risk_frame([-5.] * 12)
        self.assertTrue((daily_improvement(risk_base, risk_new, 'risk') > 0).all())
        self.assertGreater(day_bootstrap_ci(daily_improvement(risk_base, risk_new, 'risk'))[0], 0)

    def test_threshold_and_ci_are_both_required(self):
        self.assertTrue(passes_improvement(.05, [.001, .1], .05))
        self.assertFalse(passes_improvement(.049, [.001, .1], .05))
        self.assertFalse(passes_improvement(.10, [-.001, .1], .05))
        self.assertFalse(passes_improvement(.10, None, .05))
        self.assertFalse(passes_improvement(float('nan'), [.001, .1], .05))

    def test_more_mean_profit_does_not_override_worse_tail(self):
        old = self.risk_frame([-10.] * 20)
        new = self.risk_frame([20.] * 18 + [-100.] * 2, limit=1000.)
        self.assertGreater(new.profit.mean(), old.profit.mean())
        guard = risk_validation_guard(old, new)
        self.assertEqual(guard['status'], 'blocked_downside_worsened')
        self.assertFalse(guard['checks']['cvar_not_worse'])
        self.assertFalse(guard['checks']['exceed_fraction_not_worse'])
        self.assertFalse(risk_evidence_passed(guard))

    def test_feasibility_and_missing_evidence_block(self):
        old = self.risk_frame([-10.] * 20)
        new = self.risk_frame([-5.] * 20, reference_scenario_feasible=[False] + [True] * 19,
                              reference_scenario_exceed=[.2] + [0.] * 19)
        self.assertEqual(risk_validation_guard(old, new)['status'], 'blocked_downside_worsened')
        self.assertEqual(risk_validation_guard(old.iloc[:2], new.iloc[:2])['status'], 'insufficient_evidence')
        self.assertEqual(risk_validation_guard(old, new.drop(columns='reference_limit'))['status'], 'insufficient_evidence')
        self.assertEqual(risk_validation_guard(old, self.risk_frame([-5.] * 20, period_hours=23))['status'], 'insufficient_evidence')
        bad = new.copy(); bad.index = [new.index[0]] * len(new)
        self.assertEqual(risk_validation_guard(old, bad)['status'], 'insufficient_evidence')

    def test_nonworsening_guard_and_serialized_gate(self):
        old = self.risk_frame([-10.] * 20)
        new = self.risk_frame([-5.] * 20)
        result = risk_validation_guard(old, new)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(risk_evidence_passed(result))
        result['candidate']['cvar90_daily_loss_cny'] = 999.
        self.assertFalse(risk_evidence_passed(result))
        self.assertAlmostEqual(empirical_cvar90(np.arange(1, 16)), (15 + .5 * 14) / 1.5)

    def test_relaxed_confidence_cannot_mask_five_to_thirteen_scenario_breaches(self):
        from reference_risk import evaluate_reference
        import importlib.util
        path = Path(__file__).resolve().parents[2] / 'coefficient_opt/coefficient_optimization.py'
        spec = importlib.util.spec_from_file_location('reference_test_optimizer', path)
        optimizer = importlib.util.module_from_spec(spec); spec.loader.exec_module(optimizer)
        config = {'confidence': .9, 'error_shrink': 1., 'backtest_window_days': 90,
                  'deviation_rule': 'profit_recovery', 'deviation_lower_ratio': .8,
                  'deviation_upper_ratio': 1.2, 'cvar_alpha': .9, 'lambda_risk': .5}
        f = np.full((90, 24), 10.); load = f.copy(); load[:5] = 14.
        spread = np.full((90, 24), -100.); spread[5:13] = 100.
        scenarios = (f, load, spread, np.zeros(24), None, 'a' * 64)
        base = evaluate_reference(np.ones(24), scenarios, config, 1000., optimizer.pnl)
        candidate = evaluate_reference(np.full(24, 1.2), scenarios, config, 1000., optimizer.pnl)
        own = optimizer.risk(optimizer.pnl(np.full(24, 1.2), f, load, spread, dict(config, confidence=.85)),
                             dict(config, confidence=.85), 1000.)
        self.assertTrue(own['feasible'])  # Candidate-owned relaxed flag caused the original regression.
        self.assertAlmostEqual(base['reference_scenario_exceed'], 5 / 90)
        self.assertAlmostEqual(candidate['reference_scenario_exceed'], 13 / 90)
        self.assertTrue(base['reference_scenario_feasible'])
        self.assertFalse(candidate['reference_scenario_feasible'])
        old = self.risk_frame([-10.] * 20, **base)
        new = self.risk_frame([-5.] * 20, **candidate)
        new['feasible'] = True; new['limit'] = 1000000.  # Neither flag nor own auto limit can rescue it.
        guard = risk_validation_guard(old, new)
        self.assertEqual(guard['status'], 'blocked_downside_worsened')
        self.assertFalse(guard['checks']['reference_scenario_exceed_not_worse'])
        self.assertFalse(risk_evidence_passed(guard))
        guard['status'] = 'passed'  # apply rechecks the serialized daily rows, not this label.
        self.assertFalse(risk_evidence_passed(guard))

    def test_reference_versions_constraints_and_receipt_must_match(self):
        old = self.risk_frame([-10.] * 20)
        for updates in [{'reference_confidence': .85}, {'reference_error_shrink': .5},
                        {'reference_window_days': 120}, {'reference_scenario_hash': 'd' * 64},
                        {'reference_baseline_sha256': 'e' * 64}]:
            with self.subTest(updates=updates):
                new = self.risk_frame([-5.] * 20, **updates)
                self.assertEqual(risk_validation_guard(old, new)['status'], 'insufficient_evidence')

    def test_calendar_candidate_validation(self):
        validate_value('training.calendar_features', 'basic')
        validate_value('training.calendar_features', 'enhanced')
        with self.assertRaises(ValueError):
            validate_value('training.calendar_features', 'future')


if __name__ == '__main__':
    unittest.main()
