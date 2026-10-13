"""Privacy-safe synthetic EV-018 metric tests."""
import unittest

from m5daylog.vad_eval import aggregate_speech, score_fixture


class EV018Tests(unittest.TestCase):
    def test_speech_recall_intersection_is_sample_weighted(self):
        a = score_fixture("quiet", "quiet", total_samples=16000,
                          reference=[(100, 1100), (2000, 3000)],
                          predicted=[(150, 1100), (2000, 2950)])
        b = score_fixture("noisy", "noise", total_samples=16000,
                          reference=[(0, 100)],
                          predicted=[])
        self.assertEqual(a.reference_samples, 2000)
        self.assertEqual(a.recovered_samples, 1900)
        self.assertAlmostEqual(a.recall, 0.95)
        whole = aggregate_speech([a, b])
        self.assertEqual(whole["reference_samples"], 2100)
        self.assertAlmostEqual(whole["speech_recall"], 1900 / 2100)
        self.assertEqual(whole["missed_utterances"], 1)
        self.assertFalse(whole["recall_pass"])

    def test_onset_late_uses_first_overlap_and_nonnegative_samples(self):
        x = score_fixture("quiet", "quiet", total_samples=16000,
                          reference=[(400, 1000), (1600, 2500)],
                          predicted=[(0, 900), (2000, 2500)])
        self.assertEqual(x.onset_median_ms, 12.5)
        self.assertEqual(x.onset_p95_ms, 23.75)
        self.assertEqual(x.missed_utterances, 0)

    def test_aggregate_onset_gates_use_individual_utterances(self):
        a = score_fixture("quiet", "quiet", total_samples=10000,
                          reference=[(0, 1000), (2000, 3000)],
                          predicted=[(0, 1000), (2400, 3000)])
        b = score_fixture("noisy", "noise", total_samples=10000,
                          reference=[(0, 1000)],
                          predicted=[(800, 1000)])
        result = aggregate_speech([a, b])
        self.assertEqual(result["onset_median_ms"], 25.0)
        self.assertAlmostEqual(result["onset_p95_ms"], 47.5)
        self.assertTrue(result["onset_pass"])
        missed = score_fixture("missed", "quiet", total_samples=100,
                               reference=[(10, 20)], predicted=[])
        self.assertEqual(aggregate_speech([missed])["missed_utterances"], 1)
        self.assertFalse(aggregate_speech([missed])["onset_pass"])

    def test_silence_excluded_from_recall_and_false_positive_reported(self):
        silence = score_fixture("silence", "digital-silence",
                                total_samples=16000, reference=[],
                                predicted=[(0, 1600)])
        self.assertIsNone(silence.recall)
        self.assertEqual(silence.false_positive_samples, 1600)
        self.assertAlmostEqual(silence.false_positive_ratio, 0.1)
        self.assertFalse(aggregate_speech([silence])["recall_pass"])

    def test_touching_intervals_are_union_for_recall(self):
        result = score_fixture("touch", "quiet", total_samples=100,
                               reference=[(10, 20), (20, 30)],
                               predicted=[(10, 30)])
        self.assertEqual(result.reference_samples, 20)
        self.assertEqual(result.recovered_samples, 20)
        self.assertEqual(result.recall, 1.0)

    def test_invalid_ranges_fail_closed(self):
        with self.assertRaises(ValueError):
            score_fixture("invalid", "quiet", total_samples=100,
                          reference=[(50, 60), (10, 20)], predicted=[])
        with self.assertRaises(ValueError):
            score_fixture("invalid", "quiet", total_samples=100,
                          reference=[], predicted=[(0, 101)])


if __name__ == "__main__":
    unittest.main()
