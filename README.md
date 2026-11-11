# ffmpeg-convert

Convert media files in folders with ffmpeg:

- `.mov` → `.mp4` (fast stream copy, falling back to H.264/AAC re-encode)
- `.heic` → `.jpg`
- `.mp4` → `.mkv` via stream copy (`mp4-to-mkv`)
- `.mov` → `.mkv` via stream copy (`mov-to-mkv`)

Converted files are saved next to the originals.

## Requirements

- [ffmpeg](https://ffmpeg.org/) on `PATH` (`brew install ffmpeg`)
- [uv](https://docs.astral.sh/uv/)

## How to run

```sh
uv run ffmpeg.py convert folder1 folder2 ...
uv run ffmpeg.py mp4-to-mkv folder1 folder2 ...
uv run ffmpeg.py mov-to-mkv folder1 folder2 ...
```

Options:

- `--no-recursive` / `-R` — only process the top folder
- `--overwrite` / `-f` — replace existing converted files
- `--delete-original` — delete each original after a successful conversion
