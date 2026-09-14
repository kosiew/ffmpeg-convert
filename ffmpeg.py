import os
import shutil
import subprocess
from typing import List

import typer

# Source extension -> target extension
CONVERSIONS = {
    ".mov": ".mp4",
    ".heic": ".jpg",
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


CONVERTERS = {
    ".mov": convert_mov_to_mp4,
    ".heic": convert_heic_to_jpg,
}


def find_convertible_files(folder_path, recursive=True):
    """Find all files in a folder whose extension has a known conversion."""
    matches = []
    for root, dirs, files in os.walk(folder_path):
        for file_name in sorted(files):
            if os.path.splitext(file_name)[1].lower() in CONVERSIONS:
                matches.append(os.path.join(root, file_name))
        if not recursive:
            dirs.clear()
    return matches


def convert_file(source_path, overwrite=False, delete_original=False):
    """
    Convert a single file to its target format, saved next to the original.

    Returns "converted", "skipped" or "failed".
    """
    base_name, ext = os.path.splitext(source_path)
    ext = ext.lower()
    target_path = base_name + CONVERSIONS[ext]

    if os.path.exists(target_path) and not overwrite:
        print(f"Skipped (already exists): {target_path}")
        return "skipped"

    print(f"Converting: {source_path} → {os.path.basename(target_path)}")
    if not CONVERTERS[ext](source_path, target_path):
        # Don't leave a partial output file behind
        if os.path.exists(target_path):
            os.remove(target_path)
        print(f"Failed: {source_path}")
        return "failed"

    # Preserve the original's modification time so the files still sort by date
    stat = os.stat(source_path)
    os.utime(target_path, (stat.st_atime, stat.st_mtime))

    if delete_original:
        os.remove(source_path)
        print(f"Deleted original: {source_path}")
    return "converted"


def process_folder(folder_path, recursive=True, overwrite=False, delete_original=False):
    """Convert all supported files in a folder; return counts per outcome."""
    counts = {"converted": 0, "skipped": 0, "failed": 0}
    for source_path in find_convertible_files(folder_path, recursive):
        outcome = convert_file(source_path, overwrite, delete_original)
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
            counts = process_folder(folder, recursive, overwrite, delete_original)
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


if __name__ == "__main__":
    app()
