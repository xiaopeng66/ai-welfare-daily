"""Regression tests for the read-only rules report (standard-library unittest)."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ab_rules


class MutatingRules:
    @staticmethod
    def is_relevant_title(title):
        return title != 'rejected'

    @staticmethod
    def score_topic(row):
        row['tags'] = ['兑换码']
        row['score'] = 1
        return row


class BaseRules:
    @staticmethod
    def is_relevant_title(title):
        return title != 'base-rejected'

    @staticmethod
    def score_topic(row):
        row['tags'] = ['旧标签']
        row['score'] = 7
        return row


class ReportTests(unittest.TestCase):
    def test_store_retag_counts_mutating_scorer_without_altering_rows(self):
        rows = [{'title': 'CDK', 'tags': [], 'score': 0},
                {'title': 'same', 'tags': ['兑换码'], 'score': 1},
                {'title': 'score only', 'tags': ['兑换码'], 'score': 99},
                {'title': 'rejected', 'tags': [], 'score': 0}]
        original = copy.deepcopy(rows)
        output = io.StringIO()
        # Exercise the CLI path, including a deserialised row shared with the
        # caller. A real mutating scorer used to overwrite its comparison side.
        with patch.object(sys, 'argv', ['ab_rules', '--corpus', 'corpus', '--store', 'store']), \
             patch.object(ab_rules, 'load_base', return_value=(MutatingRules, 'base-temp')), \
             patch.object(ab_rules, 'load_module', return_value=MutatingRules), \
             patch.object(ab_rules, 'unique_titles', return_value=['CDK']), \
             patch.object(ab_rules.os.path, 'exists', return_value=True), \
             patch.object(ab_rules, 'open', create=True, return_value=io.StringIO('\n'.join(json.dumps(r) for r in rows))), \
             patch.object(ab_rules.json, 'loads', side_effect=rows), \
             patch.object(ab_rules.os, 'unlink'), contextlib.redirect_stdout(output):
            ab_rules.main()
        self.assertIn('new gate would drop 1, re-tag would change 2', output.getvalue())
        self.assertEqual(rows, original)
        self.assertIn('score only', output.getvalue())
        self.assertIn('CDK', output.getvalue())


    def test_store_jsonl_uses_real_base_gate(self):
        rows = [{'title': 'base-rejected', 'url': 'https://a/1', 'tags': ['旧标签'], 'score': 7}]
        output = io.StringIO()
        class Buffer(io.StringIO):
            def close(self):
                pass
        report = Buffer()
        def file_for(path, *args, **kwargs):
            return report if str(path) == 'report.jsonl' else io.StringIO(json.dumps(rows[0]))
        with patch.object(sys, 'argv', ['ab_rules', '--corpus', 'corpus', '--store', 'store', '--jsonl', 'report.jsonl']), \
             patch.object(ab_rules, 'load_base', return_value=(BaseRules, 'base-temp')), \
             patch.object(ab_rules, 'load_module', return_value=MutatingRules), \
             patch.object(ab_rules, 'unique_titles', return_value=[]), \
             patch.object(ab_rules.os.path, 'exists', return_value=True), \
             patch.object(ab_rules, 'open', create=True, side_effect=file_for), \
             patch.object(ab_rules.os, 'unlink'), contextlib.redirect_stdout(output):
            ab_rules.main()
        entries = [json.loads(line) for line in report.getvalue().splitlines()]
        entry = next(row for row in entries if row['kind'] == 'store')
        self.assertFalse(entry['base_keep'])
        self.assertTrue(entry['new_keep'])
        self.assertEqual(entry['base_tags'], [])
        self.assertEqual(entry['base_score'], 0)
        self.assertEqual(entry['new_tags'], ['兑换码'])
        self.assertEqual(entry['stored_tags'], ['旧标签'])
        self.assertEqual(entry['stored_score'], 7)

    def test_jsonl_skips_invalid_line_before_writing_report(self):
        corpus = '{"title":"CDK","url":"https://a/1"}\nnot-json\nnull\n[]\n{"title":12}\n{"title":"same","url":"https://a/2"}\n'
        class Buffer(io.StringIO):
            def close(self):
                pass
        report = Buffer()
        def file_for(path, *args, **kwargs):
            return report if str(path) == 'report.jsonl' else io.StringIO(corpus)
        output = io.StringIO()
        with patch.object(sys, 'argv', ['ab_rules', '--corpus', 'corpus', '--jsonl', 'report.jsonl']), \
             patch.object(ab_rules, 'load_base', return_value=(MutatingRules, 'base-temp')), \
             patch.object(ab_rules, 'load_module', return_value=MutatingRules), \
             patch.object(ab_rules, 'open', create=True, side_effect=file_for), \
             patch.object(ab_rules.os, 'unlink'), contextlib.redirect_stdout(output):
            ab_rules.main()
        written = [json.loads(line) for line in report.getvalue().splitlines()]
        self.assertEqual([row['url'] for row in written], ['https://a/1', 'https://a/2'])
        for expected in ('skip invalid JSONL line 2', 'line 3', 'line 4', 'line 5'):
            self.assertIn(expected, output.getvalue())

    def test_jsonl_keeps_each_candidate_url_and_rejects_without_tags(self):
        candidates = [
            {'title': 'CDK', 'url': 'https://a/1', 'source': 'a'},
            {'title': 'CDK', 'url': 'https://b/2', 'source': 'b'},
            {'title': 'rejected', 'url': 'https://a/3', 'source': 'a'},
        ]
        corpus = '\n'.join(json.dumps(row) for row in candidates)
        class Buffer(io.StringIO):
            def close(self):
                pass
        output = Buffer()
        def file_for(path, *args, **kwargs):
            return output if str(path) == 'report.jsonl' else io.StringIO(corpus)
        with patch.object(sys, 'argv', ['ab_rules', '--corpus', 'corpus', '--jsonl', 'report.jsonl']), \
             patch.object(ab_rules, 'load_base', return_value=(MutatingRules, 'base-temp')), \
             patch.object(ab_rules, 'load_module', return_value=MutatingRules), \
             patch.object(ab_rules, 'open', create=True, side_effect=file_for), \
             patch.object(ab_rules.os, 'unlink'), contextlib.redirect_stdout(io.StringIO()):
            ab_rules.main()
        entries = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(entries), 3)
        self.assertEqual([row['url'] for row in entries], [row['url'] for row in candidates])
        self.assertFalse(entries[2]['new_keep'])
        self.assertEqual(entries[2]['new_tags'], [])
        self.assertEqual(entries[0]['new_tags'], ['兑换码'])
        self.assertEqual(entries[0]['base_score'], 1)

if __name__ == '__main__':
    unittest.main()
