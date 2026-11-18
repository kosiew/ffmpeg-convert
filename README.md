# ffmpeg-convert

Convert media files in folders with ffmpeg:

- `convert` — every supported file at once: `.mp4`, `.mov`, `.avi` → `.mkv`
  (stream copy, no re-encoding) and `.heic` → `.jpg`
- `mp4-to-mkv`, `mov-to-mkv`, `avi-to-mkv` — only that one video type → `.mkv`

Converted files are saved next to the originals.

## Requirements

- [ffmpeg](https://ffmpeg.org/) on `PATH` (`brew install ffmpeg`)
- [uv](https://docs.astral.sh/uv/)

## How to run

```sh
uv run ffmpeg.py convert folder1 folder2 ...
uv run ffmpeg.py mp4-to-mkv folder1 folder2 ...
uv run ffmpeg.py mov-to-mkv folder1 folder2 ...
uv run ffmpeg.py avi-to-mkv folder1 folder2 ...
```

Options:

- `--no-recursive` / `-R` — only process the top folder
- `--overwrite` / `-f` — replace existing converted files
- `--jobs N` / `-j N` — convert up to N files at once (default 4; `-j 1` for one
  at a time). If two files would produce the same output name (e.g. `clip.mp4`
  and `clip.mov` → `clip.mkv`), only the first is converted.
- `--delete-original` — move each original to the Trash after a successful conversion
  (recoverable with Finder's *Put Back*). This is the default for `mp4-to-mkv`;
  use `--keep-original` there to keep the original. `mov-to-mkv` and
  `avi-to-mkv` keep originals unless you pass `--delete-original`.

## Subtitles in MKV output

`mp4-to-mkv`, `mov-to-mkv` and `avi-to-mkv` handle subtitles in two ways:

1. **Built-in tracks.** Each subtitle track is converted to SRT and stored inside
   the `.mkv`. (MP4's `mov_text` format can't go into MKV as-is.)
2. **Separate `.srt` files.** Each text track is also saved next to the movie as
   `Movie.srt`, or `Movie.eng.srt` when the track has a language. A second track
   in the same language becomes `Movie.2.eng.srt`, so players still see the
   language.

### Why VLC shows each subtitle track twice

VLC lists the subtitle tracks built into the `.mkv`, and it also loads any `.srt`
files in the same folder that share the movie's name. A movie with 3 subtitle
tracks therefore shows **6** entries in VLC's *Subtitle Track* menu:

| VLC entry | Source                    |
| --------- | ------------------------- |
| Track 1–3 | built into the `.mkv`     |
| Track 4–6 | `Movie.eng.srt`, `Movie.2.eng.srt`, `Movie.spa.srt` |

Each pair has the same text, so you can pick either one. To see only the
built-in tracks, delete or move the `.srt` files.

### AVI files

AVI files (often DivX/Xvid) can lack timestamps, so `avi-to-mkv` generates them.
DivX/Xvid video may also use "packed B-frames", which MKV can't store, so these
are unpacked. Both steps are lossless; nothing is re-encoded.

### Cover art

An MP4's cover image is stored in the `.mkv` as an attachment (`cover.jpg`), not
as a video track. If it were a second video track, VLC would draw the built-in
subtitles onto that one-frame track, and they would never appear on the movie.
