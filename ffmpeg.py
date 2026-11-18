import json
import os
import shutil
import subprocess
from typing import List

import typer
from send2trash import send2trash

# Source extension -> target extension
CONVERSIONS = {
    ".mov": ".mp4",
    ".heic": ".jpg",
    ".mp4": ".mkv",
}


def run_ffmpeg(args):
    """Run ffmpeg quietly with the given arguments; return True on success."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip())
    return result.returncode == 0


def convert_mov_to_mp4(source_path, target_path):
    """
    Convert a MOV video to MP4.

    First try a fast remux (stream copy), which works for the H.264/HEVC + AAC
    streams most cameras and phones produce. If the streams can't go into an
    MP4 container as-is (e.g. ProRes), fall back to re-encoding with H.264/AAC.
    """
    remux_args = [
        "-i", source_path,
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy",
        "-movflags", "+faststart",
        target_path,
    ]
    if run_ffmpeg(remux_args):
        return True

    print(f"Remux failed, re-encoding: {source_path}")
    encode_args = [
        "-i", source_path,
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        target_path,
    ]
    return run_ffmpeg(encode_args)


def convert_heic_to_jpg(source_path, target_path):
    """Convert a HEIC image to a high-quality JPG."""
    return run_ffmpeg(["-i", source_path, "-frames:v", "1", "-q:v", "2", target_path])


def convert_mp4_to_mkv(source_path, target_path):
    """Remux every stream into MKV without re-encoding audio/video.

    MP4/MOV text subtitles (mov_text) can't be stored in Matroska, so
    subtitle streams are converted to SRT, which is lossless for plain text.
    """
    if not run_ffmpeg(["-i", source_path, "-map", "0", "-c", "copy", "-c:s", "srt", target_path]):
        return False
    extract_srt_files(source_path, os.path.splitext(target_path)[0])
    return True


TEXT_SUBTITLE_CODECS = {"mov_text", "subrip", "srt", "ass", "ssa", "webvtt", "text"}


def subtitle_streams(source_path):
    """Return (index, codec, language) for each subtitle stream, via ffprobe."""
    command = [
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language",
        "-of", "json", source_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except (OSError, ValueError):
        return []
    return [
        (s["index"], s.get("codec_name", ""), s.get("tags", {}).get("language", ""))
        for s in streams
    ]


def extract_srt_files(source_path, base_path):
    """
    Save each text subtitle stream as a sidecar .srt next to the output.

    Names follow the player convention: movie.srt, or movie.eng.srt when the
    stream has a language tag; clashes get a numeric suffix (movie.eng.2.srt).
    """
    args, used = ["-i", source_path], []
    for index, codec, language in subtitle_streams(source_path):
        if codec not in TEXT_SUBTITLE_CODECS:
            continue
        stem = f"{base_path}.{language}" if language and language != "und" else base_path
        name, n = f"{stem}.srt", 2
        while name in used:
            name, n = f"{stem}.{n}.srt", n + 1
        used.append(name)
        args += ["-map", f"0:{index}", "-c:s", "srt", name]
    if not used:
        return
    if run_ffmpeg(args):
        for name in used:
            print(f"Saved subtitles: {os.path.basename(name)}")
    else:
        print(f"Warning: could not extract .srt subtitles from {source_path}")


CONVERTERS = {
    ".mov": convert_mov_to_mp4,
    ".heic": convert_heic_to_jpg,
    ".mp4": convert_mp4_to_mkv,
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


def convert_file(source_path, overwrite=False, delete_original=False, target_extension=None, converter=None):
    """
    Convert a single file to its target format, saved next to the original.

    Returns "converted", "skipped" or "failed".
    """
    base_name, ext = os.path.splitext(source_path)
    ext = ext.lower()
    target_path = base_name + (target_extension or CONVERSIONS[ext])

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


def process_folder(folder_path, recursive=True, overwrite=False, delete_original=False, extensions=None, target_extension=None, converter=None):
    """Convert supported files in a folder; return counts per outcome."""
    counts = {"converted": 0, "skipped": 0, "failed": 0}
    for source_path in find_convertible_files(folder_path, recursive, extensions):
        outcome = convert_file(source_path, overwrite, delete_original, target_extension, converter)
        counts[outcome] += 1
    return counts


app = typer.Typer(help="FFmpeg Conversion Utility - Convert media files in folders")


@app.callback()
def main():
    """Keep `convert` as an explicit subcommand, like the commands in pdf.py."""


@app.command(help="Convert files in folders: .mov → .mp4, .heic → .jpg")
def convert(
    folders: List[str] = typer.Argument(
        ..., help="Folders containing files to convert"
    ),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", "-f", help="Overwrite existing converted files"
    ),
    delete_original: bool = typer.Option(
        False, "--delete-original", help="Delete the original file after a successful conversion"
    ),
):
    """
    Convert supported files in the specified folders.
    Converted files are saved next to the originals with the new extension.
    """
    if shutil.which("ffmpeg") is None:
        typer.echo("Error: ffmpeg not found on PATH. Install it with `brew install ffmpeg`.")
        raise typer.Exit(1)

    totals = {"converted": 0, "skipped": 0, "failed": 0}
    for folder in folders:
        if os.path.isdir(folder):
            typer.echo(f"Scanning folder: {folder}")
            counts = process_folder(folder, recursive, overwrite, delete_original, {".mov", ".heic"})
            for outcome, count in counts.items():
                totals[outcome] += count
        else:
            typer.echo(f"Warning: {folder} is not a valid directory.")

    typer.echo(
        f"\nConverted {totals['converted']}, skipped {totals['skipped']}, "
        f"failed {totals['failed']} file(s)."
    )
    if totals["failed"]:
        raise typer.Exit(1)


def run_batch(folders, recursive, overwrite, delete_original, extensions, target_extension=None, converter=None):
    if shutil.which("ffmpeg") is None:
        typer.echo("Error: ffmpeg not found on PATH. Install it with `brew install ffmpeg`.")
        raise typer.Exit(1)
    totals = {"converted": 0, "skipped": 0, "failed": 0}
    for folder in folders:
        if os.path.isdir(folder):
            typer.echo(f"Scanning folder: {folder}")
            counts = process_folder(folder, recursive, overwrite, delete_original, extensions, target_extension, converter)
            for outcome, count in counts.items():
                totals[outcome] += count
        else:
            typer.echo(f"Warning: {folder} is not a valid directory.")
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
):
    run_batch(folders, recursive, overwrite, delete_original, {".mp4"}, ".mkv", convert_mp4_to_mkv)


@app.command(name="mov-to-mkv", help="Remux .mov files to .mkv with stream copy")
def mov_to_mkv(
    folders: List[str] = typer.Argument(..., help="Folders containing MOV files"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r/-R", help="Also process subfolders"),
    overwrite: bool = typer.Option(False, "--overwrite", "-f", help="Overwrite existing converted files"),
    delete_original: bool = typer.Option(False, "--delete-original", help="Move original to Trash after successful conversion"),
):
    run_batch(folders, recursive, overwrite, delete_original, {".mov"}, ".mkv", convert_mp4_to_mkv)


if __name__ == "__main__":
    app()
