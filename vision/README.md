# Project JARVIS — Local Access Log Dashboard (educational demo)

A self-contained OpenCV + YOLOv8 + face_recognition demo that shows how to
structure a real-time "detect → identify → verify → announce" pipeline
without freezing the video feed and without spamming a downstream service.

**This is a simulation.** `MOCK_PROFILE_DB` in `person_access_demo.py` is a
plain Python dict, and `VerificationEngine._simulate_network_fetch()` never
opens a socket — it sleeps for a random interval and hands back data from
that dict (with an occasional simulated timeout, to exercise error handling).
There is no integration with any real background-check, people-search, or
public-records API, and this code should not be wired to one: a webcam loop
has no way to obtain the documented "permissible purpose" and consent that
services like that legally require before a lookup, and running them against
whoever happens to walk in front of a camera is exactly the misuse they
prohibit in their terms of service.

## What it demonstrates

- **Threaded camera capture** (`ThreadedVideoStream`) so `cv2.VideoCapture.read()`
  never blocks the render loop.
- **YOLOv8n person detection**, gating everything else on "is a human present".
- **A lightweight centroid tracker** (`CentroidTracker`) giving each detected
  person a stable `track_id` across frames — this is what makes "verify
  exactly once per appearance" well-defined, instead of re-triggering every
  frame.
- **face_recognition matching** against `known_faces/*.jpg` — enroll photos
  of consenting household members you control; anyone who doesn't match gets
  a per-track `unverified_visitor_<id>` handle instead of a real identity.
- **`VerificationEngine`**: a `ThreadPoolExecutor`-backed, lock-guarded state
  machine (`PENDING → IN_PROGRESS → COMPLETE/FAILED`) that fires the mock
  lookup exactly once per identity for the process lifetime.
- **`Announcer`**: speaks the result via `pyttsx3` if installed, otherwise
  prints it — swap in your own TTS call here.
- **`AccessLogger`**: appends each completed (simulated) verification to a
  local JSONL file, purely as a demo of "managing an access log" — nothing
  leaves the machine.

## Setup

```bash
pip install -r vision/requirements-vision.txt
```

`face_recognition` depends on `dlib`, which needs a C++ toolchain to build:

```bash
# macOS
brew install cmake
# Ubuntu/Debian
sudo apt-get install -y cmake build-essential
```

If the `dlib` build fails, try `pip install dlib-binary` (prebuilt wheels for
common platforms) before `pip install face_recognition`.

Optionally copy `.env.example` to `.env` and adjust simulation parameters
(camera index, YOLO confidence threshold, simulated latency/failure rate).
`python-dotenv` is optional — without it the script just uses the defaults.

## Enrolling known people

Drop a clear, front-facing photo of each consenting household member into
`known_faces/`, named after them:

```
known_faces/
  Jane_Doe.jpg
  Alex_Smith.jpg
```

The display name JARVIS uses is derived from the filename (underscores →
spaces, title-cased). A photo with no detectable face is skipped with a
warning at startup, not a crash.

## Run

```bash
python vision/person_access_demo.py
```

Press **`q`** in the video window, or **Ctrl+C** in the terminal, to exit —
either path releases the camera, closes the OpenCV window, and joins the
verification worker pool before the process ends.

## Extending this safely

- To make this a real "welcome home" feature: keep everything as-is and just
  replace `Announcer.announce()` with your assistant's real TTS call — no
  background-check machinery needed for that.
- To log unrecognized visitors across sessions (not just this run): see this
  repo's `actions/visitor_log.py`, which already does cross-session
  clustering/dedup of unknown faces without ever resolving them to a real
  identity.
- If you have an actual, legally permissible screening use case (e.g., a
  staffed visitor checkpoint with ID capture and signed disclosure), that
  needs an explicit consent step in the flow *before* any real lookup fires
  — it is a materially different design from "camera sees a face, lookup
  fires automatically," which this demo intentionally does not implement.
