import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('benchmark', Path(__file__).with_name('orchestration-benchmark.py'))
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class BenchmarkTest(unittest.TestCase):
    def test_keeps_last_usage_and_exact_worker_punctuation(self):
        worker = {'id': 'w', 'status': 'success', 'rawInput': {'isSubagent': True, 'agentName': 'Worker', 'taskTitle': 'A', 'prompt': 'Return A'},
                  'rawOutput': {'trajectory': [{'type': 'result', 'text': 'A.'}],
                                'usage': {'inputTokens': 4, 'outputTokens': 2, 'cacheReadTokens': 0, 'cacheWriteTokens': 0, 'totalCost': 0}}}
        events = [{'status': {'id': 'first', 'tokensIn': 5}},
                  {'status': {'id': 'first', 'tokensIn': 10, 'tokensOut': 2, 'cacheReads': 3, 'cacheWrites': 0, 'cost': 0}},
                  {'card': worker}, {'card': {'kind': 'task_completion', 'status': 'success', 'body': 'A'}}]
        answer, parent, workers = benchmark.parse_events('\n'.join(json.dumps({'content': event}) for event in events))
        self.assertEqual(parent, (10, 2, 3, 0, 0))
        self.assertEqual(answer, 'A')
        self.assertEqual(workers[0][5], 'A.')
        self.assertAlmostEqual(benchmark.estimate(parent), 0.00000203)
        self.assertIsNone(benchmark.estimate((10, 2, None, 0, 0)))

    def test_advanced_judges(self):
        good = """WITH ranked AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY revision DESC) AS rank FROM usage
        ) SELECT team, SUM(COALESCE(tokens, 0)), COUNT(*) FROM ranked WHERE rank=1
          GROUP BY team ORDER BY 2 DESC, 1 ASC"""
        self.assertTrue(benchmark.verify_coding(good))
        self.assertFalse(benchmark.verify_coding('SELECT team, SUM(tokens), COUNT(*) FROM usage GROUP BY team'))
        self.assertFalse(benchmark.verify_coding('SELECT load_extension("bad")'))
        self.assertTrue(benchmark.verify_research(json.dumps(dict(winner='Atlas', monthly_usd=130,
                            atlas_sources=['B', 'A'], birch_adjusted_usd=140, cedar_p95_ms=170))))
        self.assertFalse(benchmark.verify_research('{"winner":"Birch"}'))
        self.assertIsNone(benchmark.estimate((10, 2, 3, 0, 0), 'gpt-6.2-sol'))

    def test_dashboard_escapes_stored_content(self):
        with sqlite3.connect(':memory:') as db, tempfile.TemporaryDirectory() as directory:
            db.row_factory = sqlite3.Row
            db.executescript(benchmark.SCHEMA)
            db.execute("INSERT INTO comparisons VALUES ('test','<script>bad()</script>','[]')")
            output = Path(directory) / 'index.html'
            benchmark.render(db, output)
            html = output.read_text()
            self.assertIn(benchmark.escape('<script>bad()</script>'), html)
            self.assertNotIn('<script>', html)


if __name__ == '__main__':
    unittest.main()
