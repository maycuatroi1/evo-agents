"""The project registry: registering a project, the projects a caller sees and their access to one, archiving a
project, a self-managed project's admins, and the org chart's routes. The flat route modules ``projects``,
``project_admin``, ``project_archive`` and ``org`` keep their APIRouter; each handler passes the engine and the
request's parts to a public function of ``service``, which opens the transaction with ``UnitOfWork``; the SQL is in
``repository`` and the shapes in ``models`` (contract hub-project_registry-layers in pyproject.toml)."""
