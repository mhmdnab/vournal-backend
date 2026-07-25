# Transcription samples (quality gate)

Drop your test recordings **in this folder**, then run `python test_transcription.py`
from the repo root. Audio files here are gitignored — only this README is committed.

## What to record

Record three short clips (roughly 20–60 seconds each) the way a real user would
speak into the app — rambling, casual, not scripted. The point is to see whether
transcription survives real Lebanese / mixed speech before we wire anything.

1. **English** — a clear English ramble about your day, with a couple of things
   you "need to" do.
2. **Lebanese Arabic** — a spoken Lebanese Arabic ramble. (Expect it to come back
   as Arabic script — that's fine, the extractor already handles Arabic.)
3. **Code-switched English + Arabic** — the realistic case: mix English and
   Lebanese Arabic naturally in one clip.

## Format

- **`.m4a` is ideal** (that's what Expo records), but `.mp3`, `.wav`, `.webm`,
  `.mp4`, `.mpga`, `.mpeg` all work.
- Keep each file under 25 MB (a minute of voice is tiny, so this is not a concern).
- Name them however you like, e.g. `english.m4a`, `lebanese.m4a`, `mixed.m4a`.

## Recording tips (macOS)

- **Voice Memos** app → record → share/export as `.m4a` into this folder.
- Or **QuickTime Player** → File → New Audio Recording → save, then export.

Once the files are here and your STT key is set in `.env` (`GROQ_API_KEY` by
default, since `STT_PROVIDER=groq`), the test script prints the detected
language, duration, and full transcript for each clip.

Get a free Groq API key at https://console.groq.com/keys.
