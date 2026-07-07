# Storage Layout

Generated and uploaded artifacts are organized by purpose:

- `input/videos`:
  Uploaded source videos.
- `input/rubrics`:
  Uploaded rubric files.
- `output/audio`:
  Extracted MP3 files from uploaded videos.
- `output/audio_professionalism`:
  Audio professionalism JSON outputs (openSMILE eGeMAPSv02 features + timing metrics).
- `output/whisperx`:
  Raw WhisperX output files (session subfolders).
- `output/transcripts`:
  Normalized transcript JSON files used by the frontend.
- `output/communication_scores`:
  Communication rubric scoring JSON.
- `output/scores`:
  Clinical analysis rubric scoring JSON.
- `sessions`:
  Session metadata JSON, including runtime and output references.

All of these paths are used by the FastAPI backend in `fastapi_backend/app/`.
