# Getting started

Choose a starting point:

- [Run your first plan](#run-your-first-plan) with a team hub and a worker.
- [Try the knowledge graph locally](#try-the-knowledge-graph-locally) without a hub or model account.

## Run your first plan

You'll need:

- Access to a hub, through your team or an [invite request](https://github.com/maycuatroi1/evo-agents/issues/new?template=invite.yml).
  Invite requests are public GitHub issues. Leave out private project details and credentials.
  To run your own hub, follow the [deployment guide](hub-deployment.md).
- Python 3.11+, [uv](https://docs.astral.sh/uv/), Git and tmux on the worker machine.
- Claude Code, Codex CLI or opencode installed and authenticated, and Git access to the project's repos.
- macOS or Linux for the worker daemon; use WSL on Windows. The CLI and local graph also run on Windows directly.

The worker runs code with the access of its operating-system account. On a shared machine, use the
[credential setup for workers](workers.md#a-machine-others-administer) before dispatching work.

### Install and sign in

Accept your hub invitation in the browser, then run:

```sh
uv tool install 'evo-ak[graphify,worker]'
evo-agents hub login --url https://hub.example.org
```

Replace `https://hub.example.org` with your hub's URL. The login command starts GitHub device sign-in;
complete it in the browser. `evo-ak` is the package name, while `evo-agents` is the command.

### Connect a project

If you joined an existing project, skip to [Connect a worker](#connect-a-worker).

To register your own project, you need a register invitation or hub-admin access. In the repo you want to work on,
or a folder whose direct subdirectories are Git repos, run:

```sh
cd ~/code/acme-tools
evo-agents harness init --project acme
evo-agents harness validate
evo-agents hub project register
```

The harness groups the repos into a project. `harness init` writes `harness.yaml` and `knowledge.yaml` without
overwriting existing files. Review the repo paths and remotes, then commit those files in your project.
For custom sources and labels, see [Graph setup](kg-setup.md).

### Connect a worker

On the hub's **Workers** page, create a pairing code and use the join command it shows:

```sh
evo-agents worker join --url https://hub.example.org --code XXXX-XXXX
evo-agents worker service install
evo-agents worker status
```

Use your hub URL and the actual pairing code. The service runs in the background. Check that the worker appears
online in the hub and that `worker status` lists your coding-agent runtime.

Run `evo-agents worker doctor` to review the machine's credential and access findings. Fix findings before sharing
it with others. A service installed through `uv tool install` can update itself to nightly builds when idle; use the
worker's version lock if you need it to stay on one release ([worker updates](workers.md#updating-itself-to-nightly)).

### Create and run a plan

Open the project's **Plans** page and choose **New plan**. Describe a small change and how you want it checked.
An agent on your worker writes the plan. Read it, adjust the scope if needed, then choose **Run plan**.

Open the run to see its steps, log and verification results. **Monitor** follows several runs at once. Decisions
that require your answer appear in **Inbox**, according to your [autonomy settings](autonomy.md).

For dispatch commands, logs, stopping a run and credential setup, see [Operations](operations.md).

## Connect your coding agent

For interactive sessions in your own terminal, plugins give the agent access to project plans, memories and the
graph. You do not need to install these in your terminal to dispatch work from the hub's web interface.

### Claude Code

```sh
claude plugin marketplace add https://github.com/maycuatroi1/evo-agents
claude plugin install evo-hub@evo-agents
claude plugin install evo-kg@evo-agents
```

### Codex

```sh
codex plugin marketplace add https://github.com/maycuatroi1/evo-agents
codex plugin add evo-hub@evo-agents
```

After signing in with `evo-agents hub login`, start a session in your project's directory. The plugins bind to that
project. Review the hook trust prompt when your agent first loads them. The [agent guide](agents.md) describes the
MCP tools, hooks and data they send to the hub.

## Try the knowledge graph locally

You need Python 3.11+, uv and Git. No hub login or model account is needed for this walkthrough. In a Git repo with
at least one commit, run:

```sh
uv tool install 'evo-ak[graphify]'
cd /path/to/your-repo
evo-agents harness init
evo-agents harness validate
evo-agents kg sync --build
evo-agents kg status
evo-agents kg query kg_search "README" --backend local
```

Replace the path and search text with your repo and a file or symbol it contains. A successful build reports its
sources and coverage; the search returns matching node IDs. `harness init` writes configuration into the directory
and leaves existing configuration untouched. The graph itself lives under `~/.evo/kg/`, outside your repo.

Git sources index committed code. With an origin remote, the generated configuration reads the remote default
branch; review `knowledge.yaml` if you want to index another branch. A folder of Git repos works too: each repo
gets a source in the same graph. [Graph setup](kg-setup.md) covers source selection and an example of tracing a
schema dependency across repos.

## Other installation options

Use pip instead of uv:

```sh
pip install 'evo-ak[graphify,worker]'
```

Omit `worker` for graph-only use. Without `graphify`, code-symbol extraction covers Python only; the extra adds
parsers for other languages. A source that requires the graphify backend fails to build if the extra is missing.

For unreleased builds:

```sh
uv tool install 'evo-ak[graphify,worker] @ git+https://github.com/maycuatroi1/evo-agents@main'
uv tool install 'evo-ak[graphify,worker] @ git+https://github.com/maycuatroi1/evo-agents@nightly'
```

Choose one: `main` follows the current source, and `nightly` follows the nightly tag. For an editable checkout and
local tests, see [Contributing](../CONTRIBUTING.md).
