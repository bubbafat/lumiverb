"""The pools and AI jobs today's producers share (declarations only, like a
producer's ``__init__``). A producer with a resource of its own declares
its Pool (and AiJob) in its own folder instead.

Slots are today's limits, which the brain handles (Robert, Oct 9): scans
one at a time (each is parallel inside), probes 2, analysis proxies as this
machine says, CLIP and face detection taking turns on the GPU, scenes 1,
and the AI machines as many at once as Settings → AI says.
"""

from src.producers.contract import DECODES, WHILE_RUNNING, AiJob, Pool

VISION_JOB = AiJob("vision", "Descriptions & text", guard="src.processing.vision_guard:VisionGuard")
# The scheduler's own Whisper does transcripts with nothing to set up.
TRANSCRIPTS_JOB = AiJob("transcripts", "Transcripts", guard="src.processing.transcript_guard:TranscriptGuard",
                        default_model="small", built_in=True, per_request=2)

SCANS = Pool("scan")  # the scheduler's scan pass (not a producer's)
PROBES = Pool("probe", slots=2)
# Each render decoding on this machine's GPU takes a request from the AI
# machines sharing it.
RENDERS = Pool("render", sized_by="renders", gpu_hold=DECODES)
# CLIP and face detection take turns on the brain's GPU, beside the decodes
# and the AI machine that may share it.
GPU = Pool("gpu")
SCENES = Pool("scenes", gpu_hold=WHILE_RUNNING)
VISION = Pool("vision", job=VISION_JOB)
TRANSCRIPTS = Pool("transcripts", job=TRANSCRIPTS_JOB)
