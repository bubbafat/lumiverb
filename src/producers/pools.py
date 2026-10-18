"""The pools and AI jobs today's producers share (declarations only, like a
producer's ``__init__``). A producer with a resource of its own declares
its Pool (and AiJob) in its own folder instead.

Slots are today's limits, which prod handles (Robert, Oct 9): scans
one at a time (each is parallel inside), probes 2, analysis proxies as this
machine says, CLIP and face detection taking turns on the GPU, scenes 1,
and the AI machines as many at once as Settings → AI says. CLIP is an AI
job (ADR-022): the built-in machine (the scheduler's own model, on this
GPU) does it as it always has, and Lumiverb's AI workers share it; faces
too, photos' and videos' frames alike.
"""

from src.producers.contract import DECODES, WHILE_RUNNING, AiJob, Pool
from src.shared.whisper_models import BUILT_IN_MODELS as WHISPER_MODELS

VISION_JOB = AiJob("vision", "Descriptions & text", guard="src.processing.vision_guard:VisionGuard")
# The scheduler's own Whisper does transcripts with nothing to set up.
TRANSCRIPTS_JOB = AiJob("transcripts", "Transcripts", guard="src.processing.transcript_guard:TranscriptGuard",
                        default_model="small", built_in=True, built_in_models=tuple(WHISPER_MODELS), per_request=2)
# Photos' CLIP vectors: the scheduler's own CLIP, or a Lumiverb AI worker's (lumiverb-server ai-worker).
CLIP_JOB = AiJob("clip", "Visual search (CLIP)", guard="src.processing.clip_guard:ClipGuard",
                 default_model="ViT-B-32", built_in=True, built_in_models=("ViT-B-32",),
                 built_in_yields=False,
                 fixed="Another model makes every photo's vectors again; not offered yet.")
# Faces in photos and videos' frames: the scheduler's own InsightFace, or a Lumiverb AI worker's.
FACES_JOB = AiJob("faces", "Faces", guard="src.processing.faces_guard:FacesGuard", default_model="buffalo_l",
                  built_in=True, built_in_models=("buffalo_l",), built_in_yields=False,
                  fixed="buffalo_l is the one face model installed.")

SCANS = Pool("scan")  # the scheduler's scan pass (not a producer's)
PROBES = Pool("probe", slots=2)
# Each render decoding on this machine's GPU takes a request from the AI
# machines sharing it.
RENDERS = Pool("render", sized_by="renders", gpu_hold=DECODES)
SCENES = Pool("scenes", gpu_hold=WHILE_RUNNING)
VISION = Pool("vision", job=VISION_JOB)
TRANSCRIPTS = Pool("transcripts", job=TRANSCRIPTS_JOB)
CLIP = Pool("clip", job=CLIP_JOB)
FACES = Pool("faces", job=FACES_JOB)
