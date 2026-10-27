"""Only the approved G0.3b and isolated E2 module may reference Gemini."""
import tempfile
import unittest
from pathlib import Path
from src.cost_guardrail import scan_file


class G03BCostGuardrailTests(unittest.TestCase):
    def scan(self, relative, text):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            return scan_file(path, root)

    def test_approved_workflow_allows_only_gemini_key_name(self):
        self.assertEqual([], self.scan('.github/workflows/g03b-gemini-receipt.yml', 'GEMINI_API_KEY'))

    def test_gemini_stays_blocked_in_other_paths(self):
        for relative in ('src/other.py', '.github/workflows/other.yml'):
            self.assertIn('gemini_api_secret', self.scan(relative, 'GEMINI_API_KEY'))

    def test_e2_module_explicitly_allowed_but_generic_google_stays_blocked(self):
        self.assertEqual([], self.scan("src/e2_gemini_decide.py", "GEMINI_API_KEY"))
        self.assertIn("gemini_api_secret", self.scan("src/e2_gemini_decide.py", "GOOGLE_API_KEY"))

    def test_other_providers_and_generic_google_key_stay_blocked(self):
        for name in ('OPENAI_API_KEY', 'GOOGLE_API_KEY'):
            self.assertTrue(self.scan('.github/workflows/g03b-gemini-receipt.yml', name))

if __name__ == '__main__':
    unittest.main()
