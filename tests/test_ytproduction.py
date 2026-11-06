"""Produce-studio helpers that were failing jobs in /produce.

Section count, audio duration, and assembly length are pure enough to test
without ffmpeg, Groq, or a running server.
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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



class DialogueParseTests(unittest.TestCase):
    def test_letter_labels_alternate_voices(self):
        turns = ytprod._parse_dialogue("A: Welcome back.\nB: Glad to be here.\nA: Let's start.")
        self.assertEqual(turns, [(0, "Welcome back."), (1, "Glad to be here."), (0, "Let's start.")])

    def test_names_and_markdown_labels(self):
        turns = ytprod._parse_dialogue("**Tunde:** Hello.\n\n[Sarah]: Hi Tunde.")
        self.assertEqual(turns, [(0, "Hello."), (1, "Hi Tunde.")])

    def test_untagged_lines_continue_current_speaker(self):
        turns = ytprod._parse_dialogue("Host A: First line.\nStill A here.\nHost B: Now B.")
        self.assertEqual(turns, [(0, "First line. Still A here."), (1, "Now B.")])

    def test_third_label_alternates(self):
        turns = ytprod._parse_dialogue("A: one\nB: two\nC: three")
        self.assertEqual([spk for spk, _ in turns], [0, 1, 0])

    def test_plain_script_is_not_dialogue(self):
        self.assertEqual(ytprod._parse_dialogue("Just a narration.\n\nAnother paragraph."), [])

    def test_bad_second_voice_is_dropped(self):
        self.assertIsNone(ytprod._clean_voice("en-US-Guy; rm -rf"))
        self.assertEqual(ytprod._clean_voice("en-NG-EzinneNeural"), "en-NG-EzinneNeural")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class CrossfadeConcatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.durs = [2.0, 1.4, 3.08, 2.2, 1.6]
        self.segs = []
        for i, d in enumerate(self.durs):
            path = os.path.join(self.dir, f"seg_{i}.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", f"color=c=0x{i * 40:02x}2040:s=64x36:r=25", "-t", str(d),
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path],
                check=True)
            self.segs.append(path)

    def _frames(self, path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True).stdout
        return int(out.strip().strip(","))

    def test_timeline_does_not_drift_and_each_ffmpeg_opens_two_segments_at_most(self):
        out = os.path.join(self.dir, "merged.mp4")
        real_run = subprocess.run
        input_counts = []

        def spy(cmd, *a, **k):
            input_counts.append(cmd.count("-i"))
            return real_run(cmd, *a, **k)

        with mock.patch.object(ytprod.subprocess, "run", side_effect=spy):
            ytprod._ffmpeg_xfade_concat(self.segs, self.durs, out, 0.5)
        expected = (sum(self.durs) - 4 * 0.5) * 25
        self.assertLessEqual(abs(self._frames(out) - expected), 1)
        self.assertLessEqual(max(input_counts), 2)
        self.assertFalse(os.path.exists(out + ".parts"))

    def test_segment_too_short_for_the_fade_raises_so_assembly_can_fall_back(self):
        out = os.path.join(self.dir, "merged.mp4")
        with self.assertRaises(RuntimeError):
            ytprod._ffmpeg_xfade_concat(self.segs[:2], [0.2, 1.4], out, 0.5)


if __name__ == "__main__":
    unittest.main()
