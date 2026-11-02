"""Actual file decoding into existing E1/E2/E3 contracts, local storage only."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from e1_lote_prepare import LOTES, Storage, encode, prepare
from e2_gemini_decide import DecisionStore, decide, expected_record_id
from e3_commit_fixture import exercise_commit
from e3_lote_state import related_paths, verify_settlement
from e3_recovery_fixture import MARKER, MARKER_BYTES
from library_index_build import planned_indexes, validate_record
import e1_source_prepare as sources


class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'mirror'
        self.root.mkdir()
        self.store = Storage(self.root)
        self.store.create(sources.MARKER, sources.MARKER_BYTES)
        self.baseline = ('RECORD_ID: 0123456789abcdef0123\nTYPE: TECHNOLOGY\nSTATUS: REFERENCE\n'
            'DOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\nTITLE: Prior Tool\n'
            '# Prior Tool\n\n## SUMMARY\nPrior Tool retains existing knowledge.\n').encode()
        path = '00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-prior-tool.md'
        self.store.create(path, self.baseline)
        for p, raw in planned_indexes([validate_record(path, self.baseline)]).items():
            self.store.create(p, raw)
        self.source = Path(self.temp.name) / 'technical.txt'
        self.source.write_text('Pixel Rig Tool enables rigging workflows for 2D sprites.\n')

    def test_actual_text_create_commits_and_normal_indexes_route_it(self):
        stats = sources.prepare_sources(self.root, [self.source], '2026-11-01')
        self.assertEqual(stats['extracted'], 1)
        path = LOTES + '/2026-11-01/lote.json'
        import json
        lot = json.loads(self.store.read(path))
        self.assertEqual(stats['lotes_written'], 1)
        self.assertNotIn(self.source.name, encode(lot).decode())
        item = lot['itens'][0]
        markdown = ('RECORD_ID: ' + expected_record_id(item['item_id']) + '\n'
            'TYPE: TECHNOLOGY\nSTATUS: TEST\nDOMAIN: 02_WEB_DEVELOPMENT\n'
            'CATEGORY: UI_COMPONENTS\nTITLE: Pixel Rig Tool\n# Pixel Rig Tool\n'
            '\n## SUMMARY\nPixel Rig Tool enables rigging workflows for 2D sprites.\n')
        response = {'decisoes': [{'item_id': item['item_id'], 'acao': 'CREATE', 'motivo': None,
            'confianca': .95, 'record_type': 'TECHNOLOGY', 'route': '02_WEB_DEVELOPMENT/UI_COMPONENTS',
            'target_record_id': None, 'record_markdown': markdown,
            'claims': [{'text': 'Supports rigging workflows', 'evidence_quote': 'rigging workflows for 2D sprites'}]}]}
        self.assertEqual(decide(DecisionStore(self.root), responses=response, apply=True), (1, 1))
        self.store.create(MARKER, MARKER_BYTES)
        git = self.root / 'coord.git'
        subprocess.run(['git', 'init', '--bare', str(git)], capture_output=True, check=True)
        relative = path[len(LOTES)+1:]
        decisions, receipt = related_paths(relative)
        raw_lot = self.store.read(path)
        raw_decisions = self.store.read(LOTES + '/' + decisions)
        self.assertEqual(exercise_commit(self.root, git, raw_lot, relative, raw_decisions)[0],
                         'COMMITTED_RELEASED')
        self.assertTrue(verify_settlement(raw_lot, relative, raw_decisions,
            self.store.read(LOTES + '/' + receipt))['settled'])
        master = self.store.read('00_LIBRARY/MASTER_INDEX.md').decode()
        self.assertIn('technology-pixel-rig-tool.md', master)
        domain_index = self.store.read('00_LIBRARY/02_WEB_DEVELOPMENT/INDEX.md').decode()
        self.assertIn('Pixel Rig Tool', domain_index)
        renamed = self.source.with_name('renamed.txt')
        renamed.write_bytes(self.source.read_bytes())
        with patch.object(sources, 'extract_file', side_effect=AssertionError('re-extraction')):
            stats = sources.prepare_sources(self.root, [renamed], '2026-11-02')
        self.assertEqual(stats['duplicates'], 1)
        with patch('e2_gemini_decide.ask_gemini', side_effect=AssertionError('reanalysis')):
            self.assertEqual(decide(DecisionStore(self.root), apply=True), (0, 0))
        self.assertIsNone(prepare(self.store, '2026-11-02')[0])
        self.assertTrue(self.source.exists())

        # Decode a real changed source, then UPDATE through the SAME engine.
        update_source = self.source.with_name('new-knowledge.txt')
        update_source.write_text('Pixel Rig Tool supports bounded exports for sprite animation.\n')
        sources.prepare_sources(self.root, [update_source], '2026-11-02')
        update_path = LOTES + '/2026-11-02/lote.json'
        update_lot = json.loads(self.store.read(update_path))
        update_item = update_lot['itens'][0]
        record_id = expected_record_id(item['item_id'])
        self.assertEqual(update_item['matches'][0]['record_id'], record_id)
        target = '00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md'
        original = self.store.read(target)
        update_reply = {'decisoes': [{**response['decisoes'][0],
            'item_id': update_item['item_id'], 'acao': 'UPDATE', 'target_record_id': record_id,
            'record_markdown': original.decode(), 'claims': [
                {'text': 'Supports bounded exports for sprite animation',
                 'evidence_quote': 'bounded exports for sprite animation'}]}]}
        self.assertEqual(decide(DecisionStore(self.root), responses=update_reply, apply=True), (1, 1))
        relative = '2026-11-02/lote.json'
        decisions, receipt = related_paths(relative)
        raw_lot, raw_decisions = self.store.read(update_path), self.store.read(LOTES+'/'+decisions)
        self.assertEqual(exercise_commit(self.root, git, raw_lot, relative, raw_decisions)[0],
                         'COMMITTED_RELEASED')
        after = self.store.read(target)
        self.assertTrue(after.startswith(original.rstrip(b'\n')))
        self.assertIn(b'bounded exports', after)
        self.assertTrue(verify_settlement(raw_lot, relative, raw_decisions,
            self.store.read(LOTES + '/' + receipt))['settled'])

    def test_weak_and_invalid_sources_get_persistent_holds_without_model(self):
        weak = self.source.with_name('empty.txt'); weak.write_bytes(b'')
        invalid = self.source.with_name('invalid.pdf'); invalid.write_bytes(b'invalid document')
        stats = sources.prepare_sources(self.root, [weak, invalid], '2026-11-01')
        self.assertEqual(stats['held'], 2)
        self.assertEqual(stats['lotes_written'], 0)
        receipts = list((self.root / sources.RECEIPTS).glob('*.json'))
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(b'HELD' in p.read_bytes() for p in receipts))

    def test_unmarked_or_symlinked_storage_is_rejected_without_writes(self):
        (self.root / sources.MARKER).unlink()
        with self.assertRaises(sources.SourcePreparationError):
            sources.prepare_sources(self.root, [self.source], '2026-11-01')
        self.assertFalse((self.root / sources.RECEIPTS).exists())

    def test_foreign_existing_evidence_index_is_never_replaced(self):
        sources.prepare_sources(self.root, [self.source], '2026-11-01')
        index = self.root / sources.FILE_ROOT / 'index.json'
        before = index.read_bytes()
        for receipt in (self.root / sources.RECEIPTS).glob('*.json'):
            receipt.unlink()
        with self.assertRaisesRegex(sources.SourcePreparationError, 'foreign_evidence_index'):
            sources.prepare_sources(self.root, [], '2026-11-02')
        self.assertEqual(index.read_bytes(), before)

    def test_foreign_held_evidence_is_never_discarded(self):
        from ready_evidence_bridge import build_index
        foreign = build_index([], [{'package_id': 'a'*20, 'reason': 'weak_evidence'}])
        index = self.root / sources.FILE_ROOT / 'index.json'
        self.store.create(sources.FILE_ROOT + '/index.json', encode(foreign))
        before = index.read_bytes()
        with self.assertRaisesRegex(sources.SourcePreparationError, 'foreign_evidence_index'):
            sources.prepare_sources(self.root, [], '2026-11-02')
        self.assertEqual(index.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
