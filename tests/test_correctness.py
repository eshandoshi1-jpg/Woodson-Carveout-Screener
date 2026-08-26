import unittest

import pandas as pd

import conditions_engine as ce
import export_snapshot as snapshot
import fingerprint_pick as picker
import outreach


class TruthHandlingTests(unittest.TestCase):
    def test_missing_parent_flags_are_not_evidence(self):
        row = pd.Series({
            "YoY_Meaningful": float("nan"),
            "Deleveraging_Intent": float("nan"),
            "Serial_Divester_18mo": float("nan"),
            "Q_Restructuring_Hit": float("nan"),
        })
        flags = picker._parent_flags(row)
        self.assertFalse(flags["deleveraging_intent"])
        self.assertFalse(flags["serial_divester"])
        self.assertFalse(flags["restructuring"])
        self.assertIsNone(flags["rev_yoy_pct"])

    def test_missing_deleveraging_does_not_create_forced_seller(self):
        rows = pd.DataFrame([{
            "Company": "Example", "Segment": "Components Segment Member",
            "Revenue_M": 100.0, "Revenue_M_parent": 500.0,
            "Margin_pct": 12.0, "Co_Avg_Margin_pct": 12.0,
            "Is_Catchall": False, "Deleveraging_Intent": float("nan"),
            "Serial_Divester_18mo": float("nan"),
            "Q_Restructuring_Hit": float("nan"),
        }])
        result = picker.pick_for_company(rows)
        self.assertEqual(result["FP_Archetype"], "strategic_pruner")


class MandateTests(unittest.TestCase):
    def test_revenue_fit_with_unknown_ebitda_is_possible(self):
        result = snapshot.div_mandate(100_000_000, None)
        self.assertEqual(result["fit"], "possible")
        self.assertTrue(result["verify"])

    def test_known_ebitda_can_be_confirmed(self):
        result = snapshot.div_mandate(100_000_000, 10.0)
        self.assertEqual(result["fit"], "fit")
        self.assertFalse(result["verify"])


class SignalPrecisionTests(unittest.TestCase):
    def test_generic_earnings_decline_is_not_guidance_cut(self):
        self.assertFalse(ce._has_explicit_guidance_or_dividend_cut(
            "Revenue declined while the company reduced operating expenses."))

    def test_explicit_guidance_cut_is_detected(self):
        self.assertTrue(ce._has_explicit_guidance_or_dividend_cut(
            "The company lowered its full-year earnings guidance."))


class OutreachSafetyTests(unittest.TestCase):
    def test_geographic_reporting_segments_are_not_named(self):
        row = pd.Series({
            "Company": "Example Co",
            "FP_Candidate_Segment": "Japan Australia And New Zealand",
            "FP_Candidate_Share_pct": 7.0,
            "Co_Timing_Signal": "EXPLORATORY",
            "Deleveraging_Intent": False,
            "FP_Archetype": "strategic_pruner",
        })
        line = outreach.personalization(row)
        self.assertNotIn("Japan Australia", line)
        self.assertIn("non-core or underperforming business units", line)

    def test_named_business_can_still_be_used(self):
        row = pd.Series({
            "Company": "Example Co",
            "FP_Candidate_Segment": "Specialty Components",
            "FP_Candidate_Share_pct": 20.0,
            "Co_Timing_Signal": "EXPLORATORY",
            "Deleveraging_Intent": float("nan"),
            "FP_Archetype": "strategic_pruner",
        })
        line = outreach.personalization(row)
        self.assertIn("Specialty Components", line)
        self.assertIn("strategic alternatives", line)

    def test_missing_deleveraging_is_not_treated_as_true(self):
        row = pd.Series({
            "Company": "Example Co",
            "FP_Candidate_Segment": "Specialty Components",
            "FP_Candidate_Share_pct": 20.0,
            "Co_Timing_Signal": "PENDING",
            "Deleveraging_Intent": float("nan"),
            "FP_Archetype": "strategic_pruner",
        })
        line = outreach.personalization(row)
        self.assertNotIn("reducing leverage", line)


if __name__ == "__main__":
    unittest.main()
