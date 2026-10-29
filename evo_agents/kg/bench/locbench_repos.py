"""The repos of LocBench (czlll/Loc-Bench_V1 at the revision ``datasets`` pins), lowercased.

A repo of LocBench is never in tune, in any set (``tasks.repo_split``), so the parts of every set must know them
whether or not LocBench was fetched: the names are pinned here, and ``datasets.fetch`` fails when the fetched
LocBench names a repo this list lacks. Only the names of public GitHub repos: no task of the set.
"""

LOCBENCH_REPOS = frozenset(
    name.lower()
    for name in """
Agenta-AI/agenta AzureAD/microsoft-authentication-library-for-python Bears-R-Us/arkouda BerriAI/litellm
CWorthy-ocean/roms-tools Chainlit/chainlit CrossGL/crosstl DS4SD/docling Delgan/loguru Deltares/imod-python
Happy-Algorithms-League/hal-cgp Innopoints/backend JackPlowman/repo_standards_validator
JoinMarket-Org/joinmarket-clientserver Lightning-AI/pytorch-lightning NCSU-High-Powered-Rocketry-Club/AirbrakesV2
NVIDIA/NeMo NatLibFi/Annif Netflix/metaflow Open-MSS/MSS OpenEnergyPlatform/open-MaStR Ouranosinc/xclim
PlasmaPy/PlasmaPy PrefectHQ/prefect Project-MONAI/MONAI Qiskit/qiskit SYSTRAN/faster-whisper ShishirPatil/gorilla
Standard-Labs/real-intent TagStudioDev/TagStudio TransformerLensOrg/TransformerLens UCL/TLOmodel
UKPLab/sentence-transformers UXARRAY/uxarray Zulko/moviepy aio-libs/aiohttp aiortc/aiortc airbnb/knowledge-repo
albumentations-team/albumentations alexa-pi/AlexaPi ansible/ansible ansys/pymapdl avantifellows/quiz-backend
azavea/raster-vision bridgecrewio/checkov ccxt/ccxt celery/django-celery-beat certbot/certbot cisagov/manage.get.gov
ckan/ckan conan-io/conan ctc-oss/fapolicy-analyzer cupy/cupy cython/cython dask/dask django/django dmlc/dgl
duncanscanga/VDRS-Solutions explodinggradients/ragas facebookresearch/xformers feast-dev/feast flet-dev/flet
fortra/impacket fractal-analytics-platform/fractal-tasks-core freedomofpress/securedrop-client fulcrumgenomics/prymer
gammasim/simtools gaogaotiantian/viztracer getmoto/moto gitpython-developers/GitPython home-assistant/core
huggingface/accelerate huggingface/diffusers huggingface/optimum-benchmark huggingface/transformers huggingface/trl
hyeneung/tech-blog-hub-site instructor-ai/instructor intelowlproject/GreedyBear internetarchive/openlibrary
iterative/dvc ivadomed/ivadomed jax-ml/jax jazzband/django-two-factor-auth jobatabs/textec jupyterhub/oauthenticator
justin13601/ACES kedro-org/kedro keras-team/keras kiasambrook/tfl-live-tracker kornia/kornia langchain-ai/langgraph
latchset/jwcrypto locustio/locust matchms/matchms-backup mathesar-foundation/mathesar matplotlib/matplotlib
matrix-org/synapse mesonbuild/meson micropython/micropython-lib mlflow/mlflow modin-project/modin nautobot/nautobot
netbox-community/netbox networkx/networkx nltk/nltk numba/numba numpy/numpy okta/okta-jwt-verifier-python
openwisp/openwisp-users oppia/oppia optuna/optuna pandas-dev/pandas paperless-ngx/paperless-ngx phidatahq/phidata
plone/plone.restapi pm4-graders/3ES prowler-cloud/prowler pulp/pulp_rpm py-pdf/pypdf pyca/pyopenssl
pydantic/pydantic pylint-dev/pylint pypa/pip python/cpython python/mypy ranaroussi/yfinance rapidsai/dask-cuda
ray-project/ray roboflow/supervision robotframework/robotframework rq/rq rucio/rucio rwth-i6/sisyphus
sancus-tee/sancus-compiler scikit-learn/scikit-learn scipy/scipy scrapy/scrapy secdev/scapy sgkit-dev/sgkit
sktime/sktime sopel-irc/sopel spacetelescope/stcal speechbrain/speechbrain sphinx-doc/sphinx spotify/luigi
sqlfluff/sqlfluff stanfordnlp/dspy streamlit/streamlit sympy/sympy tarantool/ansible-cartridge tardis-sn/tardis
tobymao/sqlglot traceloop/openllmetry twisted/klein ultralytics/ultralytics una-auxme/paf vitalik/django-ninja
vllm-project/vllm wandb/wandb webcompat/webcompat.com xCDAT/xcdat yt-dlp/yt-dlp zarr-developers/zarr-python
zulip/zulip
""".split()
)
