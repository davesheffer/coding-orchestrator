import unittest

from solution import summarize_results


class SummarizeResultsTests(unittest.TestCase):
    def test_counts_total_duration_and_slowest(self):
        rows = [
            {'id': 'a', 'status': 'pass', 'duration_ms': 5},
            {'id': 'b', 'status': 'fail', 'duration_ms': 11},
            {'id': 'c', 'status': 'skip', 'duration_ms': 0},
        ]
        self.assertEqual(summarize_results(rows), {
            'counts': {'pass': 1, 'fail': 1, 'skip': 1},
            'duration_ms': 16, 'slowest_id': 'b',
        })
        self.assertEqual(rows[0]['duration_ms'], 5)

    def test_empty_and_equal_duration_tie(self):
        self.assertEqual(summarize_results([]), {
            'counts': {'pass': 0, 'fail': 0, 'skip': 0},
            'duration_ms': 0, 'slowest_id': None,
        })
        self.assertEqual(summarize_results([
            {'id': 'first', 'status': 'pass', 'duration_ms': 2},
            {'id': 'second', 'status': 'fail', 'duration_ms': 2},
        ])['slowest_id'], 'first')

    def test_rejects_duplicates_invalid_status_and_duration(self):
        bad_sets = [
            [{'id': 'a', 'status': 'pass', 'duration_ms': 1},
             {'id': 'a', 'status': 'fail', 'duration_ms': 1}],
            [{'id': 'a', 'status': 'unknown', 'duration_ms': 1}],
            [{'id': 'a', 'status': 'pass', 'duration_ms': -1}],
            [{'id': 'a', 'status': 'pass', 'duration_ms': 1.5}],
            [{'id': 'a', 'status': 'pass', 'duration_ms': True}],
            [{'status': 'pass', 'duration_ms': 1}],
            [None],
        ]
        for rows in bad_sets:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize_results(rows)


if __name__ == '__main__':
    unittest.main()
