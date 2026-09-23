import unittest

from solution import parse_ranges


class ParseRangesTests(unittest.TestCase):
    def test_single_values_and_ranges(self):
        self.assertEqual(parse_ranges('1-3, 7, 10-12'), [(1, 3), (7, 7), (10, 12)])

    def test_merges_overlapping_and_adjacent_intervals(self):
        self.assertEqual(parse_ranges('5-7,1-3,3-5,9,8'), [(1, 9)])

    def test_empty_text(self):
        self.assertEqual(parse_ranges('  '), [])

    def test_rejects_invalid_segments(self):
        for value in ('1,', ',1', '1,,2', '-1', '3-1', '1.5', 'a', '1-2-3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ranges(value)


if __name__ == '__main__':
    unittest.main()
