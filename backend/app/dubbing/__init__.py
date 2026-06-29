"""AutoReup dubbing core.

Clean in-process pipeline for the Chinese -> Vietnamese dubbing workflow.
Replaces the prototype ``scripts/`` probe sprawl with a single typed library.

Stage order::

    asr -> build_rail -> chunk -> translate -> edit -> render+measure -> repair -> package

ASR micro-segments are the hard rail. Anchors are timing windows only. Overflow is a
local error inside one anchor and must never push later anchors.
"""
