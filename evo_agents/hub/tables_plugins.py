"""The tables of the hub's store of plugins (0034), which ``evo_agents.hub.tables`` adds to its metadata and exposes as
its own attributes (``tables.plugins``, ``tables.plugin_versions``): queries name them there.

plugins holds each plugin the hub knows, by the id its manifest gives (``name``): the publisher of its first version,
which every later version must have too, which version is installed on the hub (NULL when none is), who installed it
and when, and, when the installed version stopped running on this release of evo-agents, why and since when the hub
turned it off (``disabled_reason``, ``disabled_at``). plugin_versions holds every version published, never changed:
the bundle's SHA-256, size and key in the blob store, the manifest as the bundle holds it, its publisher and tier, the
signature envelope beside the bundle (``evo_agents.plugins.signing``), its kind and who signed (the workflow identity
of a Sigstore certificate, or the id of an Ed25519 key), who published it and when.

The installed version is a version of the plugin: (id, installed_version) refers to plugin_versions, a key created
after both tables (``use_alter``).

The tables of 0035 place plugins in projects. project_plugins holds each plugin a project enabled, the permissions of
its manifest an admin of the project granted, who enabled it and when, who granted last and when; turning it off
deletes the row. plugin_configs holds every version of each stored layer of a plugin's config
(``evo_agents.plugins.config``): the hub's own (instance), a project's, an agent profile's (agent), never changed,
its plain values and its secrets sealed (``evo_agents.hub.server.plugin_configs``). ``plugin_tables`` gives runs its
own layer, ``plugin_config``, and run_plugins holds the version and the digest of the effective config of each plugin
enabled on a run's project when a worker claimed it.

The tables of 0036 serve the plugin host, which runs the backends of isolated plugins (``backend_tables``).
plugin_tokens holds the SHA-256 of each token the hub gave the backend of a plugin when the host started it, and when
it was revoked: starting the backend again revokes the plugin's older tokens. plugin_backends holds what the host last
said of each backend it runs: the version, its state, the isolation it has, its process id, how many times it was
started again, since when it runs, the last failure, when it is tried again, and when the host said so.

workers.plugins (0039) lists the plugins whose runtimes and environments a worker takes, as its owner set them.
runs.environment (0040) is the environment of a plugin, <plugin>/<name>, that a run's claim picked from the
environment axis of its agent profile; NULL for a run on the worker's host.

plugin_deliveries (0038) is the outbox of the extension points (``evo_agents.hub.plugin_events``): one event or
notification for one plugin in one project, its kind, the contribution it goes to, its payload, its state, tries, when
it is due, the last error, when it was made and delivered. 0038 also records the core plugins of the hub's image in
plugins and plugin_versions: signature kind ``image``, published and installed by nobody.

project_agent_types (0042) holds each agent type of a plugin a project enabled (``evo_agents.hub.task_runs``): the
plugin, the type's name, the agent profile the hub made for it, the member its runs go under (``run_as``), the names
of that member's workers they may run on, the cap of their cost per day, who enabled it and when, and when it was
turned off. runs.agent_type_id names the agent type of a task run, and runs.pulls the draft pull requests the hub opened
of its branch.

agent_questions (0100) holds each question a core plugin asked an agent type of its own (``evo_agents.hub
.question_runs``): the plugin, the project, the type, the run of kind question that answers it (its text is the run's
request), the plugin's key of it (``ref``, one question per plugin, project and ref), who asked it as the plugin names
them (``asker``, a person outside the hub), the clearance of the place the answer goes (``cleared``), and once the run
ended its outcome, the answer (only when done) and the label it carries, and when.

plugins.builtin (0147) marks canvas, srs and tasks: features of the hub that it registers itself, not plugins of the
store. A project has them on through its features (``evo_agents.hub.feature_rule``), not through project_plugins;
retired_project_plugins keeps the rows project_plugins held of them, for going back below 0147.

``core_plugin_tables`` adds the tables of the core plugins evo-ak ships that own any: srs (0046, ``tables_srs``),
tasks (0047, ``tables_tasks``) and canvas (0073, ``tables_canvas``), each as one attribute of ``evo_agents.hub.tables``
(``tables.srs``, ``tables.tasks``, ``tables.canvas``), and with them agent_questions (``tables.agent_questions``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from evo_agents.hub.tables_canvas import CanvasTables, canvas_tables
from evo_agents.hub.tables_srs import SrsTables, srs_tables
from evo_agents.hub.tables_tasks import TasksTables, tasks_tables

BACKEND_STATES = ("placing", "starting", "running", "backoff")  # plugin_backends.state (0036)
ISOLATIONS = ("uid", "process")  # plugin_backends.isolation (0036), as evo_agents.plugins.environment names them


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def plugin_tables(metadata: MetaData, runs: Table) -> tuple[Table, Table, Table, Table, Table]:
    """(plugins, plugin_versions, project_plugins, plugin_configs, run_plugins), added to ``metadata``; the column of
    a run's own layer and its environment added to ``runs``, and the plugins a worker takes the runtimes and
    environments of to ``workers``."""
    plugins = Table(
        "plugins",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("name", Text, nullable=False),
        Column("publisher", Text, nullable=False),
        Column("installed_version", Text),
        Column("installed_by", BigInteger),
        Column("installed_at", DateTime(timezone=True)),
        Column("disabled_reason", Text),
        Column("disabled_at", DateTime(timezone=True)),
        _stamp("created_at"),
        Column("builtin", Boolean, nullable=False, server_default=false()),  # a feature of the hub (0147)
        ForeignKeyConstraint(["installed_by"], ["users.id"], ondelete="RESTRICT", name="plugins_installed_by_fkey"),
        ForeignKeyConstraint(
            ["id", "installed_version"],
            ["plugin_versions.plugin_id", "plugin_versions.version"],
            ondelete="RESTRICT",
            name="plugins_installed_version_fkey",
            use_alter=True,
        ),
        PrimaryKeyConstraint("id", name="plugins_pkey"),
        UniqueConstraint("name", name="plugins_name_key"),
    )
    plugin_versions = Table(
        "plugin_versions",
        metadata,
        Column("plugin_id", BigInteger, nullable=False),
        Column("version", Text, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("r2_key", Text, nullable=False),
        Column("publisher", Text, nullable=False),
        Column("tier", Text, nullable=False),
        Column("manifest", JSONB, nullable=False),
        Column("signature", JSONB, nullable=False),
        Column("signature_kind", Text, nullable=False),
        Column("signed_by", Text, nullable=False),
        Column("published_by", BigInteger),  # NULL for a core plugin of the hub's image (0038)
        _stamp("published_at"),
        ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_versions_plugin_id_fkey"),
        ForeignKeyConstraint(
            ["published_by"], ["users.id"], ondelete="RESTRICT", name="plugin_versions_published_by_fkey"
        ),
        PrimaryKeyConstraint("plugin_id", "version", name="plugin_versions_pkey"),
    )
    return plugins, plugin_versions, *_project_tables(metadata, runs)


def _fkey(column: str, target: str, table: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [target], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _project_tables(metadata: MetaData, runs: Table) -> tuple[Table, Table, Table]:
    project_plugins = Table(
        "project_plugins",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("permissions", ARRAY(Text), nullable=False, server_default="{}"),
        Column("records_level", Text),  # 0169: the highest level a call with no caller touches records at
        Column("enabled_by", BigInteger, nullable=False),
        _stamp("enabled_at"),
        Column("granted_by", BigInteger, nullable=False),
        _stamp("granted_at"),
        _fkey("project_id", "projects.id", "project_plugins"),
        _fkey("plugin_id", "plugins.id", "project_plugins", "CASCADE"),
        _fkey("enabled_by", "users.id", "project_plugins"),
        _fkey("granted_by", "users.id", "project_plugins"),
        PrimaryKeyConstraint("project_id", "plugin_id", name="project_plugins_pkey"),
        Index("project_plugins_plugin_idx", "plugin_id", "project_id"),
    )
    plugin_configs = Table(
        "plugin_configs",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("layer", Text, nullable=False),
        Column("project_id", BigInteger),
        Column("profile_id", BigInteger),
        Column("version", Integer, nullable=False),
        Column("values", JSONB, nullable=False, server_default="{}"),
        Column("secrets", JSONB, nullable=False, server_default="{}"),
        Column("rolled_back_to", Integer),
        Column("changed_by", BigInteger, nullable=False),
        _stamp("changed_at"),
        _fkey("plugin_id", "plugins.id", "plugin_configs", "CASCADE"),
        _fkey("project_id", "projects.id", "plugin_configs"),
        _fkey("profile_id", "agent_profiles.id", "plugin_configs"),
        _fkey("changed_by", "users.id", "plugin_configs"),
        PrimaryKeyConstraint("id", name="plugin_configs_pkey"),
        UniqueConstraint(
            "plugin_id",
            "layer",
            "project_id",
            "profile_id",
            "version",
            name="plugin_configs_version_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    runs.append_column(Column("plugin_config", JSONB))
    # the plugins whose runtimes and environments a worker takes, as its owner set them (0039)
    metadata.tables["workers"].append_column(Column("plugins", ARRAY(Text), nullable=False, server_default="{}"))
    runs.append_column(Column("environment", Text))  # the environment of a plugin the claim picked; NULL: host (0040)
    run_plugins = Table(
        "run_plugins",
        metadata,
        Column("run_id", BigInteger, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("version", Text, nullable=False),
        Column("config_digest", Text, nullable=False),
        _stamp("recorded_at"),
        _fkey("run_id", "runs.id", "run_plugins", "CASCADE"),
        ForeignKeyConstraint(
            ["plugin_id", "version"],
            ["plugin_versions.plugin_id", "plugin_versions.version"],
            ondelete="RESTRICT",
            name="run_plugins_version_fkey",
        ),
        PrimaryKeyConstraint("run_id", "plugin_id", name="run_plugins_pkey"),
    )
    return project_plugins, plugin_configs, run_plugins


def retired_table(metadata: MetaData) -> Table:
    """retired_project_plugins (0147): the rows of project_plugins of canvas, srs and tasks as they were when those
    became features of the hub, kept so that going back below 0147 puts them back as they were."""
    return Table(
        "retired_project_plugins",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("permissions", ARRAY(Text), nullable=False, server_default="{}"),
        Column("enabled_by", BigInteger, nullable=False),
        Column("enabled_at", DateTime(timezone=True), nullable=False),
        Column("granted_by", BigInteger, nullable=False),
        Column("granted_at", DateTime(timezone=True), nullable=False),
        Column("settings_version", Integer),  # the version of the project's features 0147 added; NULL: none
        _stamp("retired_at"),
        _fkey("project_id", "projects.id", "retired_project_plugins"),
        _fkey("plugin_id", "plugins.id", "retired_project_plugins", "CASCADE"),
        _fkey("enabled_by", "users.id", "retired_project_plugins"),
        _fkey("granted_by", "users.id", "retired_project_plugins"),
        PrimaryKeyConstraint("project_id", "plugin_id", name="retired_project_plugins_pkey"),
    )


def backend_tables(metadata: MetaData, runs: Table) -> tuple[Table, Table, Table, Table]:
    """(plugin_tokens, plugin_backends, plugin_deliveries, project_agent_types), added to ``metadata``."""
    plugin_tokens = Table(
        "plugin_tokens",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("token_hash", Text, nullable=False),
        _stamp("created_at"),
        Column("revoked_at", DateTime(timezone=True)),
        _fkey("plugin_id", "plugins.id", "plugin_tokens", "CASCADE"),
        PrimaryKeyConstraint("id", name="plugin_tokens_pkey"),
        UniqueConstraint("token_hash", name="plugin_tokens_token_hash_key"),
        Index("plugin_tokens_plugin_idx", "plugin_id"),
    )
    plugin_backends = Table(
        "plugin_backends",
        metadata,
        Column("plugin_id", BigInteger, nullable=False),
        Column("version", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("isolation", Text, nullable=False),
        Column("pid", Integer),
        Column("restarts", Integer, nullable=False, server_default="0"),
        Column("started_at", DateTime(timezone=True)),
        Column("error", Text),
        Column("retry_at", DateTime(timezone=True)),
        _stamp("reported_at"),
        _fkey("plugin_id", "plugins.id", "plugin_backends", "CASCADE"),
        PrimaryKeyConstraint("plugin_id", name="plugin_backends_pkey"),
    )
    return plugin_tokens, plugin_backends, _deliveries(metadata), _agent_types(metadata, runs)


def _deliveries(metadata: MetaData) -> Table:
    deliveries = Table(
        "plugin_deliveries",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("contribution", Text, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("state", Text, nullable=False, server_default="pending"),
        Column("attempts", SmallInteger, nullable=False, server_default="0"),
        _stamp("next_at"),
        Column("last_error", Text),
        _stamp("created_at"),
        Column("delivered_at", DateTime(timezone=True)),
        _fkey("plugin_id", "plugins.id", "plugin_deliveries", "CASCADE"),
        _fkey("project_id", "projects.id", "plugin_deliveries", "CASCADE"),
        PrimaryKeyConstraint("id", name="plugin_deliveries_pkey"),
        Index("plugin_deliveries_plugin_idx", "plugin_id", "id"),
        Index("plugin_deliveries_created_idx", "created_at"),
    )
    Index(
        "plugin_deliveries_due_idx",
        deliveries.c.next_at,
        deliveries.c.id,
        postgresql_where=deliveries.c.state == "pending",
    )
    return deliveries


def _agent_types(metadata: MetaData, runs: Table) -> Table:
    agent_types = Table(
        "project_agent_types",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("profile_id", BigInteger, nullable=False),
        Column("run_as", BigInteger, nullable=False),
        Column("workers", ARRAY(Text), nullable=False, server_default="{}"),
        Column("daily_usd", Double, nullable=False),
        Column("enabled_by", BigInteger, nullable=False),
        _stamp("enabled_at"),
        Column("disabled_at", DateTime(timezone=True)),
        _fkey("project_id", "projects.id", "project_agent_types"),
        _fkey("plugin_id", "plugins.id", "project_agent_types", "CASCADE"),
        _fkey("profile_id", "agent_profiles.id", "project_agent_types"),
        _fkey("run_as", "users.id", "project_agent_types"),
        _fkey("enabled_by", "users.id", "project_agent_types"),
        PrimaryKeyConstraint("id", name="project_agent_types_pkey"),
        UniqueConstraint("project_id", "plugin_id", "name", name="project_agent_types_name_key"),
        Index("project_agent_types_plugin_idx", "plugin_id", "project_id"),
    )
    runs.append_column(Column("agent_type_id", BigInteger))  # the agent type of a task run (0042)
    runs.append_column(Column("pulls", JSONB))  # the draft pull requests of a task run's branch (0042)
    runs.append_constraint(_fkey("agent_type_id", "project_agent_types.id", "runs"))
    typed = runs.c.agent_type_id.is_not(None)
    Index("runs_agent_type_idx", runs.c.agent_type_id, runs.c.queued_at, postgresql_where=typed)
    return agent_types


def question_table(metadata: MetaData) -> Table:
    """agent_questions (0100): the questions of core plugins to their agent types."""
    return Table(
        "agent_questions",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("agent_type_id", BigInteger, nullable=False),
        Column("run_id", BigInteger, nullable=False),
        Column("ref", Text, nullable=False),
        Column("asker", Text, nullable=False),
        Column("cleared", JSONB, nullable=False),
        Column("outcome", Text),
        Column("answer", Text),
        Column("label", JSONB),
        _stamp("created_at"),
        Column("answered_at", DateTime(timezone=True)),
        _fkey("plugin_id", "plugins.id", "agent_questions", "CASCADE"),
        _fkey("project_id", "projects.id", "agent_questions", "CASCADE"),
        _fkey("agent_type_id", "project_agent_types.id", "agent_questions", "CASCADE"),
        _fkey("run_id", "runs.id", "agent_questions", "CASCADE"),
        PrimaryKeyConstraint("id", name="agent_questions_pkey"),
        UniqueConstraint("run_id", name="agent_questions_run_id_key"),
        UniqueConstraint("plugin_id", "project_id", "ref", name="agent_questions_ref_key"),
    )


def core_plugin_tables(metadata: MetaData) -> tuple[SrsTables, TasksTables, CanvasTables, Table]:
    """The tables of the core plugins srs, tasks and canvas, and agent_questions, the questions core plugins ask their
    agent types (``question_table``), added to ``metadata`` (tasks links to srs_items and runs; canvas names runs and
    chat_messages)."""
    return srs_tables(metadata), tasks_tables(metadata), canvas_tables(metadata), question_table(metadata)
