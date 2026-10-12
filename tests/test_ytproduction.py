"""Produce-studio helpers that were failing jobs in /produce.

Section count, audio duration, and assembly length are pure enough to test
without ffmpeg, Groq, or a running server.
"""
import importlib.util
import os
import unittest
from pathlib import Path

os.environ.setdefault("YTPROD_OUTPUT_DIR", "/tmp/ytprod-test-output")

_PATH = Path(__file__).resolve().parent.parent / "vendor" / "ytproduction" / "app.py"
_spec = importlib.util.spec_from_file_location("ytprod_under_test", _PATH)
ytprod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ytprod)


def _words(text):
    return text.split()


class SplitScriptTests(unittest.TestCase):
    def test_exact_paragraph_count_keeps_every_word(self):
        script = "Alpha one two.\n\nBeta three four.\n\nGamma five six.\n\nDelta seven eight."
        sections = ytprod._split_script_sections(script, 4)
        self.assertEqual(len(sections), 4)
        joined = " ".join(s["text"] for s in sections)
        self.assertEqual(_words(joined), _words(script.replace("\n", " ")))

    def test_more_paragraphs_than_requested_still_covers_the_script(self):
        script = "\n\n".join(f"Paragraph {i} has words." for i in range(6))
        sections = ytprod._split_script_sections(script, 3)
        self.assertEqual(len(sections), 3)
        self.assertEqual(_words(" ".join(s["text"] for s in sections)), _words(script))

    def test_single_block_still_yields_requested_count(self):
        words = [f"w{i}" for i in range(24)]
        script = " ".join(words)
        sections = ytprod._split_script_sections(script, 6)
        self.assertEqual(len(sections), 6)
        self.assertEqual(_words(" ".join(s["text"] for s in sections)), words)

    def test_long_script_does_not_depend_on_model_limits(self):
        script = "\n\n".join(["word " * 40 for _ in range(30)])
        sections = ytprod._split_script_sections(script, 12)
        self.assertEqual(len(sections), 12)
        self.assertEqual(
            _words(" ".join(s["text"] for s in sections)),
            _words(script),
        )


class DurationProbeTests(unittest.TestCase):
    def test_format_duration_used_when_streams_omit_it(self):
        data = {
            "format": {"duration": "12.5"},
            "streams": [{"codec_type": "audio"}],
        }
        self.assertEqual(ytprod._duration_from_probe(data), 12.5)

    def test_ignores_missing_and_na_durations(self):
        data = {
            "format": {"duration": "N/A"},
            "streams": [{"duration": None}, {"duration": "3.25"}],
        }
        self.assertEqual(ytprod._duration_from_probe(data), 3.25)

    def test_empty_probe_is_zero(self):
        self.assertEqual(ytprod._duration_from_probe({}), 0.0)


class AssemblyDurationTests(unittest.TestCase):
    def test_crossfade_extends_last_segment_so_narration_is_not_cut(self):
        sections = [{"start": i * 10, "end": (i + 1) * 10} for i in range(6)]
        durs, xfade, use = ytprod._assembly_durations(sections, True, 0.5)
        self.assertTrue(use)
        self.assertEqual(xfade, 0.5)
        # 6 natural 10s segments, 5 fades, plus 0.25s slack on the last.
        self.assertAlmostEqual(durs[-1], 10 + 5 * 0.5 + 0.25)
        video_after_fades = sum(durs) - 5 * xfade
        narration = 60
        self.assertGreater(video_after_fades, narration)

    def test_short_sections_shrink_or_drop_the_fade(self):
        sections = [{"start": i * 0.5, "end": (i + 1) * 0.5} for i in range(4)]
        durs, xfade, use = ytprod._assembly_durations(sections, True, 0.5)
        self.assertTrue(use)
        self.assertLess(xfade, 0.5)
        self.assertGreaterEqual(xfade, 0.12)
        video_after_fades = sum(durs) - (len(durs) - 1) * xfade
        self.assertGreater(video_after_fades, 2.0)


if __name__ == "__main__":
    unittest.main()
