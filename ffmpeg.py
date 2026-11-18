import json
import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from send2trash import send2trash

# Files converted at once by default. Remuxing is mostly disk-bound, so a few
# parallel jobs help; many more just compete for the same disk.
DEFAULT_JOBS = 4

# Source extension -> target extension
CONVERSIONS = {
    ".mp4": ".mkv",
    ".mov": ".mkv",
    ".m4v": ".mkv",
    ".avi": ".mkv",
    ".heic": ".jpg",
}


# Per-thread handle to this file's progress bar (set by run_batch), so the
# converters don't need to pass it around.
_current = threading.local()


def set_progress_total(seconds):
    """Tell this thread's progress bar how long the media is, if known."""
    reporter = getattr(_current, "reporter", None)
    if reporter and seconds:
        reporter(total=seconds)


def run_ffmpeg(args, progress=False):
    """
    Run ffmpeg quietly with the given arguments; return True on success.

    With progress=True, ffmpeg's -progress output drives this thread's
    progress bar (if any).
    """
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    reporter = getattr(_current, "reporter", None) if progress else None
    if reporter is None:
        result = subprocess.run([*command, *args], capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr.strip())
        return result.returncode == 0

    # stderr goes to a temp file so a chatty ffmpeg can't block on a full pipe
    # while we're reading progress lines from stdout.
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(
            [*command, "-progress", "pipe:1", "-nostats", *args],
            stdout=subprocess.PIPE, stderr=errors, text=True,
        )
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                reporter(completed=int(value) / 1_000_000)
        process.wait()
        if process.returncode != 0:
            errors.seek(0)
            print(errors.read().strip())
    return process.returncode == 0


def probe_duration(source_path):
    """Return the media duration in seconds, or None if unknown."""
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", source_path]
    try:
        return float(subprocess.run(command, capture_output=True, text=True).stdout.strip())
    except (OSError, ValueError):
        return None


def convert_heic_to_jpg(source_path, target_path):
    """Convert a HEIC image to a high-quality JPG."""
    return run_ffmpeg(["-i", source_path, "-frames:v", "1", "-q:v", "2", target_path])


def convert_to_mkv(source_path, target_path):
    """Remux MP4/M4V/MOV/AVI into MKV without re-encoding audio/video.

    - mov_text subtitles can't be stored in Matroska, so subtitles become SRT.
    - Cover art (an MP4 "attached picture") is stored as an MKV attachment, not
      copied as a second video track: VLC renders embedded subtitles onto that
      one-frame track, so they never appear on the movie.
    - Data streams (e.g. MOV timecode) are dropped; Matroska can't hold them.
    - AVI often lacks timestamps, so they are generated (+genpts), and
      DivX/Xvid "packed B-frames" are unpacked, which Matroska requires.
    """
    streams = probe_streams(source_path)
    is_avi = source_path.lower().endswith(".avi")
    with tempfile.TemporaryDirectory() as temp_dir:
        args = [
            *(["-fflags", "+genpts"] if is_avi else []),
            "-i", source_path,
            "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
            "-c", "copy", "-c:s", "srt",
        ]
        video_codecs = {
            st.get("codec_name") for st in streams
            if st.get("codec_type") == "video" and not st.get("disposition", {}).get("attached_pic")
        }
        if video_codecs == {"mpeg4"}:
            args += ["-bsf:v", "mpeg4_unpack_bframes"]
        for n, (path, mime) in enumerate(extract_cover_art(source_path, streams, temp_dir)):
            args += [
                "-attach", path,
                f"-metadata:s:t:{n}", f"mimetype={mime}",
                f"-metadata:s:t:{n}", f"filename={os.path.basename(path)}",
            ]
        set_progress_total(probe_duration(source_path))
        if not run_ffmpeg([*args, target_path], progress=True):
            return False
    extract_srt_files(source_path, os.path.splitext(target_path)[0], streams)
    return True


TEXT_SUBTITLE_CODECS = {"mov_text", "subrip", "srt", "ass", "ssa", "webvtt", "text"}
COVER_ART_TYPES = {"mjpeg": ("jpg", "image/jpeg"), "png": ("png", "image/png")}


def probe_streams(source_path):
    """Return ffprobe's stream list (index, type, codec, language, dispositions)."""
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language:stream_disposition=attached_pic",
        "-of", "json", source_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        return json.loads(result.stdout or "{}").get("streams", [])
    except (OSError, ValueError):
        return []


def extract_cover_art(source_path, streams, temp_dir):
    """Save attached pictures to temp_dir; return [(path, mimetype)]."""
    covers = []
    for stream in streams:
        if not stream.get("disposition", {}).get("attached_pic"):
            continue
        file_type = COVER_ART_TYPES.get(stream.get("codec_name"))
        if not file_type:
            continue
        ext, mime = file_type
        suffix = f"{len(covers) + 1}" if covers else ""
        path = os.path.join(temp_dir, f"cover{suffix}.{ext}")
        if run_ffmpeg(["-i", source_path, "-map", f"0:{stream['index']}", "-c", "copy", "-frames:v", "1", "-update", "1", path]):
            covers.append((path, mime))
    return covers


def extract_srt_files(source_path, base_path, streams):
    """
    Save each text subtitle stream as a sidecar .srt next to the output.

    Names follow the player convention: movie.srt, or movie.eng.srt when the
    stream has a language tag. Later tracks with the same language get a number
    before the language (movie.2.eng.srt) so players still read the language.
    """
    args, used = ["-i", source_path], []
    for stream in streams:
        if stream.get("codec_type") != "subtitle" or stream.get("codec_name") not in TEXT_SUBTITLE_CODECS:
            continue
        language = stream.get("tags", {}).get("language", "")
        lang_suffix = f".{language}" if language and language != "und" else ""
        name, n = f"{base_path}{lang_suffix}.srt", 2
        while name in used:
            name, n = f"{base_path}.{n}{lang_suffix}.srt", n + 1
        used.append(name)
        args += ["-map", f"0:{stream['index']}", "-c:s", "srt", name]
    if not used:
        return
    if run_ffmpeg(args):
        for name in used:
            print(f"Saved subtitles: {os.path.basename(name)}")
    else:
        print(f"Warning: could not extract .srt subtitles from {source_path}")


CONVERTERS = {
    ".mp4": convert_to_mkv,
    ".mov": convert_to_mkv,
    ".m4v": convert_to_mkv,
    ".avi": convert_to_mkv,
    ".heic": convert_heic_to_jpg,
}


def find_convertible_files(folder_path, recursive=True, extensions=None):
    """Find files whose extension has a known conversion, optionally filtered."""
    extensions = CONVERSIONS.keys() if extensions is None else extensions
    matches = []
    for root, dirs, files in os.walk(folder_path):
        for file_name in sorted(files):
            if os.path.splitext(file_name)[1].lower() in extensions:
                matches.append(os.path.join(root, file_name))
        if not recursive:
            dirs.clear()
    return matches


def trash_file(path):
    """Move a file to the Trash so it can be recovered."""
    send2trash(path)


def target_path_for(source_path, target_extension=None):
    """Return the output path: same folder and name, new extension."""
    base_name, ext = os.path.splitext(source_path)
    return base_name + (target_extension or CONVERSIONS[ext.lower()])


def convert_file(source_path, overwrite=False, delete_original=False, target_extension=None, converter=None):
    """
    Convert a single file to its target format, saved next to the original.

    Returns "converted", "skipped" or "failed".
    """
    ext = os.path.splitext(source_path)[1].lower()
    target_path = target_path_for(source_path, target_extension)

    if os.path.exists(target_path) and not overwrite:
        print(f"Skipped (already exists): {target_path}")
        return "skipped"

    print(f"Converting: {source_path} → {os.path.basename(target_path)}")
    if not (converter or CONVERTERS[ext])(source_path, target_path):
        # Don't leave a partial output file behind
        if os.path.exists(target_path):
            os.remove(target_path)
        print(f"Failed: {source_path}")
        return "failed"

    # Preserve the original's modification time so the files still sort by date
    stat = os.stat(source_path)
    os.utime(target_path, (stat.st_atime, stat.st_mtime))

    if delete_original:
        trash_file(source_path)
        print(f"Moved original to Trash: {source_path}")
    return "converted"


app = typer.Typer(help="FFmpeg Conversion Utility - Convert media files in folders")


@app.callback()
def main():
    """Keep `convert` as an explicit subcommand, like the commands in pdf.py."""


@app.command(help="Convert files in folders: .mp4/.m4v/.mov/.avi → .mkv, .heic → .jpg")
def convert(
    folders: List[str] = typer.Argument(..., help="Folders containing files to convert"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"),
    overwrite: bool = typer.Option(False, "--overwrite", "-f", help="Overwrite existing converted files"),
    delete_original: bool = typer.Option(False, "--delete-original/--keep-original", help="Move original to Trash after successful conversion"),
    jobs: int = typer.Option(DEFAULT_JOBS, "--jobs", "-j", min=1, help="Number of files to convert at once"),
):
    """
    Convert supported files in the specified folders.
    Converted files are saved next to the originals with the new extension.
    """
    run_batch(folders, recursive, overwrite, delete_original, set(CONVERSIONS), jobs=jobs)


def shorten(name, width=40):
    """Trim long file names for the progress display, keeping the end."""
    return name if len(name) <= width else "…" + name[-(width - 1):]


def run_batch(folders, recursive, overwrite, delete_original, extensions, target_extension=None, converter=None, jobs=DEFAULT_JOBS):
    """Convert matching files in the folders, up to `jobs` at a time."""
    if shutil.which("ffmpeg") is None:
        typer.echo("Error: ffmpeg not found on PATH. Install it with `brew install ffmpeg`.")
        raise typer.Exit(1)

    sources = []
    for folder in folders:
        if os.path.isdir(folder):
            typer.echo(f"Scanning folder: {folder}")
            sources += find_convertible_files(folder, recursive, extensions)
        else:
            typer.echo(f"Warning: {folder} is not a valid directory.")

    totals = {"converted": 0, "skipped": 0, "failed": 0}

    # clip.mp4 and clip.mov both become clip.mkv; running them at once would
    # write the same file twice, so only the first one is converted. Compare
    # case-insensitively because macOS file names are.
    claimed, tasks = set(), []
    for source_path in sources:
        key = target_path_for(source_path, target_extension).lower()
        if key in claimed:
            print(f"Skipped (another file converts to the same name): {source_path}")
            totals["skipped"] += 1
            continue
        claimed.add(key)
        tasks.append(source_path)

    console = Console()
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        disable=not console.is_terminal,
        transient=True,
    )
    overall = progress.add_task(f"Files 0/{len(tasks)}", total=len(tasks))
    done, done_lock = [0], threading.Lock()

    def convert_one(source_path):
        # One bar per running file; filled in from ffmpeg's progress output
        task = progress.add_task(f"  {shorten(os.path.basename(source_path))}", total=None)
        _current.reporter = lambda **fields: progress.update(task, **fields)
        try:
            return convert_file(source_path, overwrite, delete_original, target_extension, converter)
        finally:
            _current.reporter = None
            progress.remove_task(task)
            with done_lock:
                done[0] += 1
                progress.update(overall, advance=1, description=f"Files {done[0]}/{len(tasks)}")

    pool = ThreadPoolExecutor(max_workers=jobs)
    try:
        with progress:
            for outcome in pool.map(convert_one, tasks):
                totals[outcome] += 1
    except KeyboardInterrupt:
        # Ctrl-C also stops the running ffmpeg processes; don't start queued ones
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()

    typer.echo(f"\nConverted {totals['converted']}, skipped {totals['skipped']}, failed {totals['failed']} file(s).")
    if totals["failed"]:
        raise typer.Exit(1)


@app.command(name="mp4-to-mkv", help="Remux .mp4 files to .mkv with stream copy")
def mp4_to_mkv(
    folders: List[str] = typer.Argument(..., help="Folders containing MP4 files"),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", "-f", help="Overwrite existing converted files"
    ),
    delete_original: bool = typer.Option(
        True, "--delete-original/--keep-original", help="Move original to Trash after successful conversion"
    ),
    jobs: int = typer.Option(DEFAULT_JOBS, "--jobs", "-j", min=1, help="Number of files to convert at once"),
):
    run_batch(folders, recursive, overwrite, delete_original, {".mp4"}, ".mkv", convert_to_mkv, jobs=jobs)


@app.command(name="mov-to-mkv", help="Remux .mov files to .mkv with stream copy")
def mov_to_mkv(
    folders: List[str] = typer.Argument(..., help="Folders containing MOV files"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"),
    overwrite: bool = typer.Option(False, "--overwrite", "-f", help="Overwrite existing converted files"),
    delete_original: bool = typer.Option(False, "--delete-original/--keep-original", help="Move original to Trash after successful conversion"),
    jobs: int = typer.Option(DEFAULT_JOBS, "--jobs", "-j", min=1, help="Number of files to convert at once"),
):
    run_batch(folders, recursive, overwrite, delete_original, {".mov"}, ".mkv", convert_to_mkv, jobs=jobs)


@app.command(name="avi-to-mkv", help="Remux .avi files to .mkv with stream copy")
def avi_to_mkv(
    folders: List[str] = typer.Argument(..., help="Folders containing AVI files"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"),
    overwrite: bool = typer.Option(False, "--overwrite", "-f", help="Overwrite existing converted files"),
    delete_original: bool = typer.Option(False, "--delete-original/--keep-original", help="Move original to Trash after successful conversion"),
    jobs: int = typer.Option(DEFAULT_JOBS, "--jobs", "-j", min=1, help="Number of files to convert at once"),
):
    run_batch(folders, recursive, overwrite, delete_original, {".avi"}, ".mkv", convert_to_mkv, jobs=jobs)


@app.command(name="m4v-to-mkv", help="Remux .m4v files to .mkv with stream copy")
def m4v_to_mkv(
    folders: List[str] = typer.Argument(..., help="Folders containing M4V files"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"),
    overwrite: bool = typer.Option(False, "--overwrite", "-f", help="Overwrite existing converted files"),
    delete_original: bool = typer.Option(False, "--delete-original/--keep-original", help="Move original to Trash after successful conversion"),
    jobs: int = typer.Option(DEFAULT_JOBS, "--jobs", "-j", min=1, help="Number of files to convert at once"),
):
    run_batch(folders, recursive, overwrite, delete_original, {".m4v"}, ".mkv", convert_to_mkv, jobs=jobs)


if __name__ == "__main__":
    app()
