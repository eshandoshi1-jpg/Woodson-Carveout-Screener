import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import conditions_engine as ce
import enrich_master as enrich
import export_snapshot as snapshot
import fingerprint_pick as picker
import outreach
import pipeline_incremental as incremental


class TruthHandlingTests(unittest.TestCase):
    def test_missing_parent_yoy_is_numeric_safe(self):
        frame = pd.DataFrame([{"Parent_Notes": "rev trend: data unavailable"}])
        result = enrich.add_yoy_flags(frame)
        self.assertTrue(pd.isna(result.loc[0, "Parent_YoY_pct"]))
        self.assertFalse(result.loc[0, "YoY_Meaningful"])

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


class BankerCRMTests(unittest.TestCase):
    def test_banker_directory_contract(self):
        crm = snapshot.load_banker_crm()
        self.assertEqual(crm["meta"]["bankCount"], 518)
        self.assertEqual(crm["meta"]["bankerCount"], 1914)
        bank_ids = {bank["id"] for bank in crm["banks"]}
        banker_ids = {person["id"] for person in crm["bankers"]}
        self.assertEqual(len(bank_ids), 518)
        self.assertEqual(len(banker_ids), 1914)
        self.assertTrue(all(person["bankId"] in bank_ids for person in crm["bankers"]))
        self.assertEqual(sum(bank["tier"] == 3 for bank in crm["banks"]), 127)
        self.assertEqual(sum(bank["tier"] == 4 for bank in crm["banks"]), 391)
        self.assertEqual(crm["meta"]["tier3BankCount"], 127)
        self.assertEqual(crm["meta"]["tier4BankCount"], 391)

    def test_banker_outreach_template_is_present_and_current(self):
        template = Path("data/woodson_app.template.html").read_text()
        self.assertIn('BANKER_SUBJECT="Intro to Woodson Equity"', template)
        self.assertIn("offices in Chicago and DC", template)
        self.assertIn("greater than $10M in EBITDA", template)
        self.assertIn("Joel Mathew", template)
        self.assertNotIn("Joel Matthew", template.replace("Joel\\s+Matthew", ""))


class SignalPrecisionTests(unittest.TestCase):
    def test_generic_earnings_decline_is_not_guidance_cut(self):
        self.assertFalse(ce._has_explicit_guidance_or_dividend_cut(
            "Revenue declined while the company reduced operating expenses."))

    def test_explicit_guidance_cut_is_detected(self):
        self.assertTrue(ce._has_explicit_guidance_or_dividend_cut(
            "The company lowered its full-year earnings guidance."))

    def test_unrelated_8k_items_do_not_fetch_documents(self):
        subs = {"filings": {"recent": {
            "form": ["8-K"],
            "filingDate": ["2026-08-01"],
            "accessionNumber": ["0000000000-26-000001"],
            "primaryDocument": ["example.htm"],
            "items": ["1.01"],
        }}}
        with patch.object(ce, "_get_text") as get_text:
            points, _note = ce._score_csuite_change(subs, "0000000000")
        self.assertEqual(points, 0)
        get_text.assert_not_called()

    def test_guidance_scan_caps_prolific_8k_filers(self):
        count = ce.MAX_8K_DOCUMENTS_PER_SIGNAL + 5
        subs = {"filings": {"recent": {
            "form": ["8-K"] * count,
            "filingDate": ["2026-08-01"] * count,
            "accessionNumber": [f"0000000000-26-{i:06d}" for i in range(count)],
            "primaryDocument": [f"example-{i}.htm" for i in range(count)],
            "items": ["8.01,9.01"] * count,
        }}}
        with patch.object(ce, "_get_text", return_value="routine update") as get_text:
            points, _note = ce._score_guidance_cut(subs, "0000000000")
        self.assertEqual(points, 0)
        self.assertEqual(get_text.call_count, ce.MAX_8K_DOCUMENTS_PER_SIGNAL)


class CikResolutionTests(unittest.TestCase):
    def test_resolve_cik_uses_installed_rapidfuzz_api(self):
        previous = ce._CIK_INDEX
        ce._CIK_INDEX = {
            "EXAMPLE CORPORATION": {
                "cik": "0000123456",
                "ticker": "EXM",
                "title": "Example Corporation",
            }
        }
        try:
            cik, ticker, score, title = ce.resolve_cik("Example Corp")
        finally:
            ce._CIK_INDEX = previous

        self.assertEqual(cik, "0000123456")
        self.assertEqual(ticker, "EXM")
        self.assertEqual(score, 100)
        self.assertEqual(title, "Example Corporation")


class IncrementalRefreshTests(unittest.TestCase):
    def test_one_company_failure_does_not_abort_other_rescans(self):
        good = pd.DataFrame([{"Company": "Good Co", "Parse_Status": "XBRL"}])

        def fake_rescan(name, _revenue, _index):
            if name == "Bad Co":
                raise RuntimeError("bad filing")
            return good.copy()

        with patch.object(incremental, "_rescan_one", side_effect=fake_rescan):
            result = incremental.rescan(["Bad Co", "Good Co"], {"Good Co": 100})

        self.assertEqual(result["Company"].tolist(), ["Good Co"])


class OutreachSafetyTests(unittest.TestCase):
    def test_sender_spelling_and_saved_browser_typo_migration(self):
        self.assertEqual(outreach.SENDER, "Joel Mathew")
        template = Path("data/woodson_app.template.html").read_text()
        self.assertIn('replace(/\\bJoel\\s+Matthew\\b/gi,"Joel Mathew")', template)
        self.assertNotIn('||"Joel Matthew"', template)

    def test_generated_email_has_no_em_dash_or_narrow_sector_claim(self):
        row = pd.Series({
            "Company": "Example Co",
            "primary_name": "Jane Smith",
            "source": "Fortune 1000",
            "HFS_Live": True,
        })
        body = outreach.build_email(row)
        self.assertIn("specializes in corporate carveouts and divestitures", body)
        self.assertIn("completed over 20 corporate carveouts", body)
        self.assertIn("came off of the TSA within the first 100 days", body)
        self.assertNotIn("I found Example Co when searching", body)
        self.assertNotIn("\u2014", body)
        self.assertNotIn("Industrials", body)
        self.assertNotIn("Manufacturing", body)
        self.assertNotIn("Business Services", body)
        self.assertNotIn("\u2014", outreach.SUBJECT)

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
