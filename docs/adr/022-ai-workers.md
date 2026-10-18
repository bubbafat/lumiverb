# ADR-022: CLIP and faces on AI machines (Lumiverb's AI worker)

## Status

Accepted (2026-10-18). Robert's order of work, item 4: "CLIP and faces on AI machines", the last of ADR-016 phase 4's endpoints. ADR-016 decided the direction ("AI inference runs at named endpoints… Embedding models (CLIP, faces) must be uniform: one per library, served from any number of endpoints"); this ADR settles how.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | The worker: `lumiverb-server ai-worker`, its API and its key | Built: `src/server/ai_worker/` (`app.py` its API, `__init__.py` its key), `lumiverb-server ai-worker`; `tests/test_ai_worker.py` (stand-ins, and the scheduler's own `list_models` over real HTTP), `tests/test_ai_worker_models.py` (`-m ai`: its vector and faces equal the scheduler's own) |
| 2 | CLIP as an AI job: the built-in machine (as today) and workers | Built: `CLIP_JOB` (`src/producers/pools.py`), `src/processing/clip_guard.py` (the built-in machine's check and its CLIP, a worker's), `src/processing/ai_worker_client.py`, the producer on the job's machines (`src/producers/clip/work.py`), the control migration giving every built-in machine `clip`, the model fixed (`AiJob.fixed`, 409 `model_fixed`, Settings › AI); a worker took a library's new photos on a throwaway install |
| 3 | Faces as an AI job: photos and video frames | Built: `FACES_JOB`, `src/processing/faces_guard.py`, `AiWorker.faces`, both face producers on the job's machines (a frame's own error skips that frame, the machines' waits the video), the control migration giving every built-in machine `faces`, its model `buffalo_l` fixed; the server's current face model is the account's faces job's (`faces.current_face_model`, through `session.info["tenant_id"]`); the old shared GPU pool is gone (the built-in machines' CLIP and faces take turns by `Models.gpu_turn`); a worker found the portrait's face and the crowd's two on a throwaway install |
| 4 | Running a worker as a service on a second machine (a Mac's launchd, a Linux unit), and the README | Not started |

## Overview

Descriptions and transcripts already run on the account's AI machines (Settings › AI): each job has one model for the account, each machine says which models it offers, takes so many requests at once, and is checked before it's sent anything; the built-in machine is the scheduler's own computer. Those machines speak APIs others already serve (Ollama, OpenAI-compatible servers). CLIP image vectors and face detection have no such API anywhere, so they still run only in the scheduler, on the server's GPU.

A second machine with a GPU (media's RTX 3080, a second Mac) could take that work if something on it answered. That something is Lumiverb's own: a small service any Lumiverb install can run.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | What runs on the other machine? | `lumiverb-server ai-worker`: a small HTTP service from the same install (Homebrew on a Mac, a Linux install's venv), loading CLIP and InsightFace exactly as the scheduler does (the same code, the same models folder). It holds no account, no database and no files: an image in, vectors or faces out. |
| 2 | How is it checked and chosen? | As every AI machine: added in Settings › AI with its URL and key, doing the jobs it's given, so many at once. It lists the models it can serve at `GET /v1/models` (the OpenAI-compatible list every machine is asked for), so the existing check (the job's model offered?), the online and offline reports and the limits all work unchanged. |
| 3 | What does it answer? | `POST /v1/lumiverb/clip` (images and the model → one vector each, with the model's id and version) and `POST /v1/lumiverb/faces` (images and the level's gates → each one's faces, as the scheduler's face process returns them). Lumiverb's own routes, under `/v1/lumiverb/`, beside the OpenAI-compatible list. |
| 4 | Who may use it? | Whoever has its key: it won't start without one. One is made on first start (random, kept in its data folder, owner-only), shown by `lumiverb-server ai-worker --show-key`, pasted into Settings › AI. A request without it is a 401. |
| 5 | Where does it listen? | Every interface on port 6366 by default (`--port`, `--host`), plain HTTP, as Ollama does: previews cross the local network as they do to an Ollama machine for descriptions today. Images are refused past 120 MP or in another format than JPEG, PNG and WebP (the server's own decoding rules, `src/server/images.py`). |
| 6 | What do the jobs become? | Two AI jobs, **Visual search (CLIP)** (`clip`) and **Faces** (`faces`), each built in: the scheduler's own computer does them by default, exactly as today, so nothing changes until a worker is added. A worker given a job shares its work with the built-in machine (which can be turned off for it, as for transcripts). |
| 7 | Uniform models? | Each job has one model for the account (CLIP `ViT-B-32`, with OpenAI's weights, named as the producer's `model` setting already is so its lineage is unchanged and nothing is made again; faces `buffalo_l`), fixed (`AiJob.fixed`), and a machine does the job only while it offers it. Lineage records the model, never the machine (ADR-016). A worker's vector differs from the built-in machine's only by floating-point rounding (CUDA, Apple's GPU, CPU): similarity is unaffected. |
| 8 | GPU sharing | The built-in machine keeps taking turns with video decoding on the server's GPU (the `shares_gpu` limit, as now). A worker's GPU is its own; if it also runs Ollama, its "at once" is the person's to set. |
| 9 | What stays on the server? | A search's own CLIP text vector and search-by-image (one short request at a time, in the API): only the producers' bulk work moves. |

## Design

### The worker (phase 1)

`src/server/ai_worker/` (a FastAPI app of its own, never the API's): `GET /v1/models` (`{"object": "list", "data": [{"id": "ViT-B-32"}, {"id": "buffalo_l"}]}`: the CLIP models it allows and the face model), `POST /v1/lumiverb/clip` (multipart `images`, form `model`), `POST /v1/lumiverb/faces` (multipart `images`, form `gates`, JSON). Requests at once are its own limit (`--at-once`, default 2): more wait. Models load on first use and are let go of when idle (the scheduler's `Models`); the face process is its own process, as in the scheduler (ONNX Runtime's leak). Errors are said, as the AI machines' are: a model it doesn't serve 404, an image it can't read 422 for that image only, out of memory 503 (the machine's fault: the scheduler sends the image elsewhere).

### The jobs (phases 2 and 3)

The producers' work asks the job's machine pool for a slot (`src/processing/ai_pool.py`), as descriptions do: the built-in machine runs today's code; a worker gets the images. The scheduler's GPU pool keeps the built-in machine's turns.

## Found while building

- **Two threads, one model, Apple's GPU (phase 2).** A worker taking two requests at once crashed (a segmentation fault, then the process gone) when both ran the same cold CLIP model on a Mac's GPU together: PyTorch's first use of a model on MPS isn't safe in two threads at once. Once warm it ran in eight threads without trouble, and two processes at once were fine (the API, the scheduler and Ollama share the GPU every day), but threads bought nothing (one thread about 200 images a second, eight about 130). So a process's models take turns, one image at a time (`Models.gpu_turn`), in the worker as in the scheduler; `--at-once` is how many requests it takes, not how many images its GPU works on.
- **A clip whose machines were all down waits up to an hour** before it's tried again (the dispatcher's `retake_after`, every AI job's); a scheduler restart, or a machine coming back to a clip not yet tried, goes on at once.

## Alternatives considered

- **A worker that pulls work, as a device does (ADR-018).** No inbound port on the worker, but work leased over the API, per account, with keys of its own: much more for the same result. The scheduler is the only orchestrator (ADR-016).
- **An existing API.** OpenAI's embeddings take text, not images; no common API finds faces.
- **Text CLIP on workers too.** One short text per search: not worth a network round trip.
