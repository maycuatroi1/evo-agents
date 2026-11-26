"""The service layer of the plugin canvas: who may do what and what a canvas shows (``reads``, ``acting``), the
writes of canvases, claims and evidence (``writes``), proposals, baselines and their decisions, the value check
(``checks``) and the judging of @strategist (``judging``), imports and exports (``transfer``). It reads and writes
through the repository and raises CanvasError on a refusal."""
