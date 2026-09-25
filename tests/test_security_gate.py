from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import security_gate


class SecurityGateTests(unittest.TestCase):
    def check_paths(self, files: dict[str, bytes]) -> list[str]:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            paths: list[Path] = []
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                paths.append(path)
            errors: list[str] = []
            with patch.object(security_gate, "ROOT", root):
                security_gate.check_tracked_files(paths, errors)
                security_gate.check_secret_patterns(paths, errors)
            return errors

    def test_rejects_live_curation_state_and_case_variants(self) -> None:
        errors = self.check_paths(
            {
                "curation/decisions.json": b'{"items": []}\n',
                "Docs/PRIVATE/notes.md": b"private material\n",
                "docs/CURRENT_STATE.md": b"private checkpoint\n",
            }
        )
        self.assertTrue(any("private-only path" in item for item in errors))
        self.assertTrue(any("generated/private path" in item for item in errors))

    def test_rejects_private_documents_urls_and_binary_files(self) -> None:
        errors = self.check_paths(
            {
                "docs/source.pdf": b"%PDF synthetic test bytes",
                "docs/source-map.json": (
                    b'{"url":"https://drive.' b'google.com/file/d/example"}\n'
                ),
                "fixtures/opaque.bin": b"\xff\xfe\x00\x00",
            }
        )
        self.assertTrue(any("file type" in item and "source.pdf" in item for item in errors))
        self.assertTrue(any("private_google_drive_url" in item for item in errors))
        self.assertTrue(any("not readable UTF-8" in item and "opaque.bin" in item for item in errors))

    def test_rejects_credential_filename_families(self) -> None:
        errors = self.check_paths(
            {
                "config/credentials-prod.json": b"{}\n",
                "config/client_secret.test.json": b"{}\n",
                "config/token-backup.json": b"{}\n",
                "config/.env.production": b"SAFE_PLACEHOLDER=1\n",
            }
        )
        self.assertEqual(4, len(errors), errors)

    def test_allows_generic_text_and_env_example(self) -> None:
        errors = self.check_paths(
            {
                "docs/design.md": b"generic documentation\n",
                ".env.example": b"TOKEN=<REDACTED>\n",
                "examples/curation-decisions.example.json": b'{"items": []}\n',
            }
        )
        self.assertEqual([], errors)

    def test_workflow_runs_for_all_pushes_and_pull_requests(self) -> None:
        workflow = (security_gate.ROOT / ".github/workflows/security-gate.yml").read_text(
            encoding="utf-8"
        )
        trigger = workflow.split("permissions:", 1)[0]
        self.assertIn("  push:\n", trigger)
        self.assertIn("  pull_request:\n", trigger)
        self.assertNotIn("paths:", trigger)

    def test_curation_workflow_reads_private_runner_temp_decisions(self) -> None:
        workflow = (
            security_gate.ROOT / ".github/workflows/curation-decision-sync.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn('"curation/decisions.json"', workflow)
        self.assertIn("99_INBOX/CURATION/DECISIONS/latest.json", workflow)
        self.assertIn('${RUNNER_TEMP}/curation-decisions.json', workflow)
        self.assertIn("--require-publish-receipt", workflow)


if __name__ == "__main__":
    unittest.main()
