# Recorded RT-DETR detections

Two 117-minute OSCE recordings of the same cohort, filmed by two cameras, run
once through `PekingU/rtdetr_v2_r18vd` at 1 sample/second and 640x356 decode.
Only the person-class detections were kept, and only those scoring >= 0.5.

| File | Source recording | Duration | Samples | Camera |
|---|---|---|---|---|
| `common_cold_session_1.json.gz` | Common Cold_Session 1.mp4 | 7022.2 s | 7022 | wide — student and patient both fully in frame |
| `hayfever_session_1.json.gz` | Hayfever_Session 1.mp4 | 6968.4 s | 6968 | tight — one student in frame, limbs intruding at the edges |

Shape (gzipped JSON):

```
{"frame_width": 640, "frame_height": 356, "sample_fps": 1.0, "model": "...",
 "frames": [[[score, x0, y0, x1, y1], ...], ...]}
```

Box coordinates are **normalised to 0..1** of the frame, so a gate expressed as
a fraction of frame height applies directly with `frame_height=1.0`.

These exist so the occupancy presets can be regression-tested against real
detector output with no GPU, no model weights and no 600 MB video: every
confidence threshold above 0.5 and every geometry gate replays from the
recording. Regenerate them only if the model or the sample rate changes — the
expected session lists in `tests/test_person_presets.py` are tied to this data.
