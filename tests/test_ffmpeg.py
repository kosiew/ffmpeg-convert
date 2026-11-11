import os
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

import ffmpeg


class Mp4ToMkvTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_command_registration(self):
        command_names = [command.name for command in ffmpeg.app.registered_commands]
        self.assertIn("mp4-to-mkv", command_names)

    def test_summary_uses_real_newline(self):
        with tempfile.TemporaryDirectory() as root, patch.object(ffmpeg.shutil, "which", return_value="ffmpeg"):
            result = self.runner.invoke(ffmpeg.app, ["mp4-to-mkv", root])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("\nConverted 0", result.output)
        self.assertNotIn("\\nConverted", result.output)

    def test_cli_converts_recursive_mp4s_only_and_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as root:
            child = os.path.join(root, "child")
            os.makedirs(child)
            sources = [os.path.join(root, "a.mp4"), os.path.join(root, "b.MP4"), os.path.join(child, "c.Mp4")]
            for source in sources:
                open(source, "w").close()
            unrelated = os.path.join(root, "unrelated.mov")
            open(unrelated, "w").close()
            timestamp = 1_600_000_000
            os.utime(sources[0], (timestamp, timestamp))
            calls = []

            def fake_run_ffmpeg(args):
                calls.append(args)
                open(args[-1], "w").close()
                return True

            with patch.object(ffmpeg.shutil, "which", return_value="ffmpeg"), patch.object(ffmpeg, "run_ffmpeg", side_effect=fake_run_ffmpeg):
                result = self.runner.invoke(ffmpeg.app, ["mp4-to-mkv", root])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(args[2:5] == ["-map", "0", "-c"] and args[5] == "copy" for args in calls))
            self.assertEqual(os.stat(os.path.join(root, "a.mkv")).st_mtime, timestamp)
            self.assertFalse(os.path.exists(os.path.join(root, "unrelated.mkv")))

    def test_no_recursive_limits_cli_to_top_folder(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "child"))
            for relative in ("top.mp4", "child/nested.mp4"):
                open(os.path.join(root, relative), "w").close()
            calls = []

            def fake_run_ffmpeg(args):
                calls.append(args)
                open(args[-1], "w").close()
                return True

            with patch.object(ffmpeg.shutil, "which", return_value="ffmpeg"), patch.object(ffmpeg, "run_ffmpeg", side_effect=fake_run_ffmpeg):
                result = self.runner.invoke(ffmpeg.app, ["mp4-to-mkv", "--no-recursive", root])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "-i")

    def test_default_skip_and_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "clip.mp4")
            target = os.path.join(root, "clip.mkv")
            open(source, "w").close()
            open(target, "w").close()
            calls = []

            def fake_run_ffmpeg(args):
                calls.append(args)
                open(args[-1], "w").close()
                return True

            with patch.object(ffmpeg.shutil, "which", return_value="ffmpeg"), patch.object(ffmpeg, "run_ffmpeg", side_effect=fake_run_ffmpeg):
                skipped = self.runner.invoke(ffmpeg.app, ["mp4-to-mkv", root])
                overwritten = self.runner.invoke(ffmpeg.app, ["mp4-to-mkv", "--overwrite", root])
            self.assertEqual(skipped.exit_code, 0, skipped.output)
            self.assertIn("skipped 1", skipped.output)
            self.assertEqual(len(calls), 1)
            self.assertEqual(overwritten.exit_code, 0, overwritten.output)
            self.assertIn("Converted 1", overwritten.output)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
