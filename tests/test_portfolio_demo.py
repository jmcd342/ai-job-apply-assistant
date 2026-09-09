"""Contract checks for a clean, offline portfolio demonstration."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demo import build_demo
from src.document_generator import DocumentGenerator


class PortfolioDemoTests(unittest.TestCase):
    def test_pipeline_generates_packets_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("socket.socket.connect", side_effect=AssertionError("Demo attempted network access")):
                database, ids = build_demo(root)
                self.assertEqual(len(ids), 3)
                self.assertEqual(database.fetch_one("SELECT COUNT(*) AS total FROM jobs")["total"], 3)
                generator = DocumentGenerator(database, root)
                for job_id in ids:
                    result = generator.generate_for_job(job_id)
                    packet = (Path(result["output_dir"]) / "application_packet.md").read_text(encoding="utf-8")
                    self.assertIn("fictional", packet)
                    self.assertNotIn("{job_title}", packet)
                    self.assertNotEqual(database.get_job_details(job_id)["status"], "applied")

    def test_interactive_demo_can_generate_and_switch_roles(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "demo_app.py"), default_timeout=120).run()
        self.assertEqual(len(app.exception), 0)
        app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Dear Hiring Team" in element.value for element in app.markdown))
        app.selectbox[0].select(2).run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(any("Dear Hiring Team" in element.value for element in app.markdown))


if __name__ == "__main__":
    unittest.main()
