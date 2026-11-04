"""Knowledge graphs for the Playwright stack: a member's push of the synthetic fixture of ``tests.hub.kg_fixture``,
and the worker's build of it, run inside ``hub_stack.py`` so a test sees a built graph without a worker running.

- POST /kg/seed {project, login, id, extra}: as that member (a writer the test granted on the project, which it
  registered with ``kg_fixture.registration``), push the fixture through the API (``evo_agents.hub.kg_push``, the blob
  uploads going to the stack's fake S3), then run the build the push queued. ``extra``, a note key, adds a note to the
  fixture, so seeding the same project again with another key pushes a run that changes the graph.
- POST /kg/build {project, login, id, run}: queue a build as that member (POST /v1/kg/{project}/builds); with
  ``run``, run it now. A project whose knowledge config was never pushed fails with the worker's own error.
- POST /kg/extract {project, login, id}: one pass of the worker's job ``hub.extract_uploads`` (every upload waiting in
  extracting is read into its source's items), then the build each read upload of ``project`` queued, run. The answer
  is GET /v1/kg/{project}/uploads as that member sees it.

A build runs as the worker's job does, through ``evo_agents.hub.kg_build.run_build`` against the stack's database
and blob store, and its procrastinate job is then marked done, the way the worker finishes one. Nothing else takes
jobs here, so a build a test queues without ``run`` stays queued. Each answer is the build as
GET /v1/kg/{project}/builds/{id} returns it.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from evo_agents.hub.client import Hub
from tests.hub.fake_github import Account
from tests.hub.kg_fixture import Harness

STACK_HOST = "e2e-stack"


class KgSeeder:
    def __init__(self, api_url: str, github, dsn: str, s3: dict, work_dir: Path):
        self.api_url = api_url
        self.github = github
        self.dsn = dsn
        self.s3 = s3
        self.work_dir = work_dir
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True)

    def member(self, body: dict) -> Hub:
        """The API as the member ``body`` names, signed in with a machine token as the CLI is."""
        token = self.github.issue_token(Account(str(body["login"]), int(body["id"])))
        signed = Hub(self.api_url).call("POST", "/v1/auth/github", {"github_token": token, "host": STACK_HOST})
        return Hub(self.api_url, signed["token"])

    def handle(self, path: str, body: dict) -> dict:
        project = str(body["project"])
        hub = self.member(body)
        if path == "/kg/seed":
            harness = Harness(self.work_dir / "machines", project)
            harness.sync(body.get("extra"))
            build = harness.push(hub).build
        elif path == "/kg/build":
            build = hub.call("POST", f"/v1/kg/{project}/builds")["build"]
            if not body.get("run"):
                return build
        elif path == "/kg/extract":
            asyncio.run(self._extract())
            for upload in hub.call("GET", f"/v1/kg/{project}/uploads")["uploads"]:
                if upload["build_id"] is not None and upload["build_status"] == "queued":
                    build = hub.call("GET", f"/v1/kg/{project}/builds/{upload['build_id']}")
                    asyncio.run(self._run(project, int(build["id"]), build.get("job_id")))
            return hub.call("GET", f"/v1/kg/{project}/uploads")
        else:
            raise KeyError(path)
        return self.finish(project, hub, build)

    def finish(self, project: str, hub: Hub, build: dict | None) -> dict:
        """Run ``build``, which a push or a POST of ``hub`` queued, as the worker would; the build as it ended."""
        if build is None:
            raise RuntimeError(f"no build of project {project} was queued")
        asyncio.run(self._run(project, int(build["id"]), build.get("job_id")))
        return hub.call("GET", f"/v1/kg/{project}/builds/{build['id']}")

    def config(self):
        from evo_agents.hub.config import HubConfig

        return HubConfig(
            dsn=self.dsn,
            data_dir=self.work_dir / "worker",
            pool_min_size=1,
            pool_max_size=2,
            pool_timeout=10.0,
            **self.s3,
        )

    async def _extract(self) -> None:
        """One pass of ``hub.extract_uploads`` as the worker runs it, its builds queued through procrastinate."""
        from evo_agents.hub.blobs import BlobStore
        from evo_agents.hub.kg_upload_extract import extract_pending
        from evo_agents.hub.worker import HubContext, connector, queue

        config = self.config()
        store = BlobStore.from_config(config)
        try:
            with queue.replace_connector(connector(config, 1)):
                await queue.open_async()
                try:
                    context = HubContext(config, queue.connector.pool, store, config.data_dir)
                    await extract_pending(context, queue.job_manager)
                finally:
                    await queue.close_async()
        finally:
            store.close()

    async def _run(self, project: str, build_id: int, job_id: int | None) -> None:
        from evo_agents.hub.blobs import BlobStore
        from evo_agents.hub.db import open_pool
        from evo_agents.hub.kg_build import run_build
        from evo_agents.hub.worker import HubContext

        config = self.config()
        store = BlobStore.from_config(config)
        pool = await open_pool(config)
        try:
            await run_build(HubContext(config, pool, store, config.data_dir), project, build_id, job_id)
            if job_id is not None:
                async with pool.connection() as conn:
                    await conn.execute("SELECT procrastinate_finish_job_v1(%s, 'succeeded', false)", (job_id,))
        finally:
            await pool.close()
            store.close()
