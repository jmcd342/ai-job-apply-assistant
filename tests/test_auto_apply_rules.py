from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.auto_apply_rules import auto_apply_block_reason, is_auto_apply_blocked


class AutoApplyRulesTests(unittest.TestCase):
    def test_is_auto_apply_blocked_matches_bank_of_china_company(self) -> None:
        self.assertTrue(is_auto_apply_blocked({"company": "Bank of China (New Zealand) Limited"}))

    def test_is_auto_apply_blocked_uses_enriched_company(self) -> None:
        self.assertTrue(is_auto_apply_blocked({"company": "", "enriched_company": "Bank of China New Zealand"}))

    def test_is_auto_apply_blocked_allows_other_companies(self) -> None:
        self.assertFalse(is_auto_apply_blocked({"company": "Industrial and Commercial Bank of China (New Zealand) Limited"}))

    def test_auto_apply_block_reason_mentions_manual_review(self) -> None:
        reason = auto_apply_block_reason({"company": "Bank of China (New Zealand) Limited"})
        self.assertIn("manual review/submission", reason.lower())


if __name__ == "__main__":
    unittest.main()
