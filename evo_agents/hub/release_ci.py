"""The CI of a release tag as the hub follows it, what a red one means, and why a cycle stops (``evo_agents.hub.releases``
describes the cycle). Pure functions over JSON values and text, on the standard library and ``evo_agents.hub.judge``,
so the api, the hub's worker and the tests share them. ``evo_agents.hub.server.release_watch`` follows the tags,
``evo_agents.hub.server.release_actions`` reads GitHub Actions, ``evo_agents.hub.server.release_cycles`` stops a cycle
and asks its owner, and ``evo_agents.hub.server.release_answer`` does what the owner answers.

A cycle follows, one row of release_tags each: the tag the hub makes on the merge commit of each of its runs, a tag a
person pushes while it is open (a version greater than every tag it follows), and the red tag a cycle that starts with
a fix opened on. The schedule's workflow (release.yml) runs on each; ``verdict`` reads its run and jobs: ``passed`` once
the run completed with success and its job PUBLISH_JOB succeeded (the cycle is then published and its owner told,
``pypi_url``), ``failed`` once it completed otherwise, ``running`` before. No run of the workflow NO_RUN_MINUTES after the
hub began to follow the tag, or a run still going MAX_RUN_HOURS after it started, stops the cycle (``timeout``).

A red tag's failure is read from its failed jobs (FAILED_CONCLUSIONS, at most MAX_FAILED_JOBS), each with the last
LOG_TAIL_BYTES of its log (``job_failure``): the ids of the tests that failed (``failed_tests``: the node ids of
pytest's FAILED and ERROR lines, the failed tests of Playwright's summary), else the job's name and the step that
failed. A job whose log matches one of INFRA_RULES and names no test failed of the runner, the network while installing,
or PyPI answering 5xx (``infra_rule``, rules on the log alone); a failure all of whose jobs did is the infrastructure's,
and the hub runs the failed jobs again, INFRA_RERUNS times at most. Any other one queues a fix run with what it read
(``failures_text``), unless the same test id failed at the cycle's tag before (``same_tests``).

A fix the hub lands must show no sign of test hacking (``release_signs``): of the signs ``judge.hack_signs`` reads, a
test removed, an assertion removed or loosened (an assertion of a test changed in any way but its numbers, an assert
made always true among them) and a skip or an xfail added, but for ``pytest.importorskip`` and a skip on one platform
that gives its reason (``allowed_skip``).

STOP_CAUSES are why a cycle stops; a stopped cycle waits for its owner's decision (STOP_OPTIONS) and goes on with
nothing until then.
"""

from __future__ import annotations

import re

from evo_agents.hub import judge
from evo_agents.hub.releases import CONTROL

CI_STATES = ("pending", "running", "passed", "failed", "timeout")  # of a tag the cycle follows
WATCHED = ("pending", "running")  # the states whose CI the hub still reads
PUBLISH_JOB = "publish"  # the job of release.yml that publishes the wheel to PyPI
PYPI_PROJECT = "evo-ak"  # the distribution release.yml publishes
NO_RUN_MINUTES = 30  # no run of the workflow this long after the hub began to follow a tag stops the cycle
MAX_RUN_HOURS = 3  # a run of the workflow going longer stops it
INFRA_RERUNS = 1  # times the hub runs the failed jobs of a red tag again, for a fault of the infrastructure
FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")  # of a job: what failed it
MAX_FAILED_JOBS = 8  # failed jobs whose logs the hub reads
LOG_TAIL_BYTES = 64 * 1024  # of each log, its end the hub reads
MAX_LOG_BYTES = 64 * 1024 * 1024  # a log longer than this is not read at all
KEPT_TAIL_CHARS = 4000  # of each log's end, what the hub keeps and the fix prompt shows
MAX_IDS = 200  # failed test ids kept of a tag
MAX_ID_CHARS = 300
MAX_FAILURE_CHARS = 24_000  # of the failures, what the fix prompt shows
ACTIONS_PERMISSION = "actions_permission"  # the problem of an installation that does not grant actions
STOP_CAUSES = (
    "run_failed",  # a run of the cycle failed
    "run_cancelled",  # a run of the cycle was cancelled while the schedule was not paused
    "refused",  # the hub refused to land a run, or a run ended without its branch
    "github",  # GitHub failed LAND_TRIES passes in a row while the hub landed a run
    "hacking",  # a run's change shows signs of test hacking
    "budget",  # what was left of the cycle's cap was under curator.MIN_RUN_USD: cycles before 0176 dropped the cap
    "same_test",  # one test id failed at two tags in a row
    "timeout",  # no run of the workflow in NO_RUN_MINUTES, or one going over MAX_RUN_HOURS
    "ci",  # the workflow ended in a way no fix forward answers: cancelled, or red with no failed job
)
STOP_OPTIONS = ("continue", "close", "pause")  # go on, close the cycle, pause the schedule

STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")  # GitHub's time before each line of a log
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S[^\n]*?\.py(?:::[^\n]+?)?)(?: - [^\n]*)?$")
PLAYWRIGHT_SUMMARY = re.compile(r"^\s*\d+ failed\s*$")
PLAYWRIGHT_TEST = re.compile(
    r"^\s*(?:\d+\)\s*)?\[([\w .-]+)\] (?:›|>) (\S+?\.(?:spec|test)\.[cm]?[jt]sx?)(?::\d+){0,2} (?:›|>) (.+?)\s*[─-]*\s*$"
)
INFRA_RULES = (
    (
        "runner_lost",
        re.compile(
            r"The runner has received a shutdown signal|lost communication with the server|The hosted runner "
            r"encountered an error|The self-hosted runner\b.*\blost\b|Runner \S+ did not connect",
            re.IGNORECASE,
        ),
    ),
    (
        "network",
        re.compile(
            r"Could not resolve host|Temporary failure in name resolution|Connection reset by peer|\bECONNRESET\b"
            r"|\bETIMEDOUT\b|\bEAI_AGAIN\b|ReadTimeoutError|ConnectTimeoutError|Failed to establish a new connection"
            r"|RemoteDisconnected|IncompleteRead|TLS handshake timeout|npm ERR! network",
            re.IGNORECASE,
        ),
    ),
    (
        "pypi_5xx",
        re.compile(
            r"(?:pypi\.org|pythonhosted\.org)\S*[^\n]*\b5\d\d\b|\b5\d\d\b[^\n]*(?:pypi\.org|pythonhosted\.org)",
            re.IGNORECASE,
        ),
    ),
)
RELEASE_SIGNS = ("test_removed", "assert_removed", "skip_added")  # of judge.SIGN_KINDS, what stops a fix
PLATFORM = re.compile(r"\bsys\.platform\b|\bos\.name\b|\bplatform\.system\s*\(|\bprocess\.platform\b")
REASON = re.compile(r"\breason\s*=\s*[rbuf]*['\"]\s*\S|,\s*['\"`][^'\"`]{3,}['\"`]\s*\)")
OTHER_SKIP = re.compile(r"\b(?:skip|skipif|xfail|skipTest|SkipTest)\b|\.(?:skip|only|fixme|todo)\s*\(")


def tag_version_text(tag: str, prefix: str) -> str:
    """The version a tag names: 0.25.0 of v0.25.0 under the prefix v."""
    return tag[len(prefix) :] if tag.startswith(prefix) else tag


def pypi_url(version: str) -> str:
    """The page of ``version`` of the distribution on PyPI."""
    return f"https://pypi.org/project/{PYPI_PROJECT}/{version}/"


def _lines(text: str):
    for line in (text or "").splitlines():
        yield ANSI.sub("", STAMP.sub("", line)).rstrip()


def _playwright(lines: list[str]) -> list[str]:
    """The tests Playwright's summary lists under ``N failed``, each ``[project] file › title``, line numbers left
    out so the same test keeps its id once lines move."""
    found, listing = [], False
    for line in lines:
        if PLAYWRIGHT_SUMMARY.match(line):
            listing = True
            continue
        test = PLAYWRIGHT_TEST.match(line) if listing else None
        if test is None:
            listing = False
            continue
        found.append(f"[{test.group(1)}] {test.group(2)} › {test.group(3)}")
    return found


def failed_tests(log: str | None) -> list[str]:
    """The ids of the tests a job's log says failed, each once in the order it names them: pytest's node ids of its
    FAILED and ERROR lines, and Playwright's failed tests."""
    lines = list(_lines(log or ""))
    found = [match.group(1).strip() for match in map(PYTEST_FAILED.match, lines) if match]
    found += _playwright(lines)
    return [CONTROL.sub(" ", item)[:MAX_ID_CHARS] for item in dict.fromkeys(found)][:MAX_IDS]


def infra_rule(log: str | None) -> str | None:
    """The name of the first of INFRA_RULES the log matches, or None."""
    text = "\n".join(_lines(log or ""))
    return next((name for name, rule in INFRA_RULES if rule.search(text)), None)


def failed_step(job: dict) -> str | None:
    """The name of the first step of ``job`` (as GitHub lists a job) that failed."""
    steps = job.get("steps") if isinstance(job.get("steps"), list) else []
    found = next((s for s in steps if isinstance(s, dict) and s.get("conclusion") in FAILED_CONCLUSIONS), None)
    return str(found.get("name")) if found and found.get("name") else None


def job_failure(job: dict, log: str | None) -> dict:
    """What the hub keeps of one failed job: its name, link and failed step, the tests its log says failed (else the
    job and the step as one id), the infrastructure rule its log matched when it names no test, and its log's end."""
    name, step = str(job.get("name") or f"job {job.get('id')}"), failed_step(job)
    tests = failed_tests(log)
    ids = tests or [f"{name}: {step or job.get('conclusion') or 'failed'}"[:MAX_ID_CHARS]]
    tail = "\n".join(CONTROL.sub(" ", line) for line in list(_lines(log or ""))[-200:])
    return {
        "job": name,
        "url": job.get("html_url") if isinstance(job.get("html_url"), str) else None,
        "step": step,
        "conclusion": job.get("conclusion"),
        "ids": ids,
        "tests": bool(tests),
        "infra": None if tests else infra_rule(log),
        "tail": tail[-KEPT_TAIL_CHARS:] if log is not None else None,
    }


def summary(jobs: list[dict]) -> dict:
    """The failure of a red tag of its failed jobs (``job_failure``): them, every id once, and whether the
    infrastructure alone failed it."""
    ids = list(dict.fromkeys(item for job in jobs for item in job["ids"]))[:MAX_IDS]
    return {"jobs": jobs, "ids": ids, "infra": bool(jobs) and all(job["infra"] for job in jobs)}


def failed_jobs(jobs: list[dict]) -> list[dict]:
    """The jobs of a run that failed it, at most MAX_FAILED_JOBS."""
    return [job for job in jobs if isinstance(job, dict) and job.get("conclusion") in FAILED_CONCLUSIONS][
        :MAX_FAILED_JOBS
    ]


def published(run: dict, jobs: list[dict]) -> bool:
    """Whether the workflow run ``run`` completed with success and its job PUBLISH_JOB succeeded."""
    publish = [job for job in jobs if isinstance(job, dict) and job.get("name") == PUBLISH_JOB]
    return (
        run.get("conclusion") == "success" and bool(publish) and all(j.get("conclusion") == "success" for j in publish)
    )


def same_tests(before: list[str] | None, now: list[str] | None) -> list[str]:
    """The test ids that failed at both of two tags in a row, in the order the second names them."""
    earlier = set(before or [])
    return [item for item in now or [] if item in earlier]


def failures_text(tag: str, workflow_url: str | None, failures: dict | None) -> str:
    """What the fix prompt says of the CI of red tag ``tag``: the run, and for each failed job its step, the tests that
    failed and the end of its log; at most MAX_FAILURE_CHARS."""
    failures = failures or {}
    lines = [f"The run of the workflow for {tag}: {workflow_url or 'its link was not read'}."]
    for job in failures.get("jobs") or []:
        where = f", step {job['step']}" if job.get("step") else ""
        lines.append(
            f"- Job {job['job']}{where} ({job.get('conclusion') or 'failed'}): {job.get('url') or ''}".rstrip()
        )
        named = "Tests that failed" if job.get("tests") else "What failed"
        lines += [f"  {named}:"] + [f"  - {item}" for item in job.get("ids") or []]
        if job.get("tail"):
            lines += ["  The end of its log:", "  ```", *("  " + line for line in job["tail"].splitlines()), "  ```"]
    text = "\n".join(lines)
    return text if len(text) <= MAX_FAILURE_CHARS else text[: MAX_FAILURE_CHARS - 40] + "\n  [cut: the rest is longer]"


def allowed_skip(text: str) -> bool:
    """Whether a line that adds a skip is one a fix may add: ``pytest.importorskip`` alone, or a skip on one platform
    that gives its reason."""
    if "importorskip" in text and not OTHER_SKIP.search(text.replace("importorskip", "")):
        return True
    return bool(PLATFORM.search(text) and REASON.search(text))


def release_signs(files: list[judge.FileDiff], repo: str) -> list[dict]:
    """The signs of test hacking in the diff ``files`` of a release run's change (see the module), each
    ``{kind, path, line, text}`` as ``judge.hack_signs`` gives them."""
    signs = judge.hack_signs(files, repo=repo, protected=[])
    return [
        sign
        for sign in signs
        if sign["kind"] in RELEASE_SIGNS and not (sign["kind"] == "skip_added" and allowed_skip(sign["text"]))
    ]


def signs_text(signs: list[dict], limit: int = 5) -> str:
    """The signs of ``release_signs`` in one line: what each is, where."""
    shown = []
    for sign in signs[:limit]:
        where = f"{sign['path']}:{sign['line']}" if sign.get("line") else sign["path"]
        shown.append(f"{judge.SIGN_KINDS.get(sign['kind'], sign['kind'])} at {where}")
    more = f" and {len(signs) - limit} more" if len(signs) > limit else ""
    return "; ".join(shown) + more
