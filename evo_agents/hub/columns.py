"""The column and key shapes the modules ``tables_<area>`` repeat, written once: a timestamp that defaults to now(),
one that is NULL until it happens, an identity ``id`` and a foreign key named the way Postgres names it
(``<table>_<column>_fkey``). Each helper returns a new object, as SQLAlchemy needs one per table."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKeyConstraint, Identity, func


def stamp(name: str) -> Column:
    """A timestamptz column that defaults to now() and is never NULL."""
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def when(name: str) -> Column:
    """A timestamptz column that is NULL until it happens."""
    return Column(name, DateTime(timezone=True))


def id_column() -> Column:
    """``id bigint GENERATED ALWAYS AS IDENTITY``."""
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def fkey(
    table: str, column: str, target: str, ondelete: str, *, field: str = "id", use_alter: bool = False
) -> ForeignKeyConstraint:
    """The key ``<table>_<column>_fkey`` from ``column`` to ``target``: a table, whose ``field`` it names, or a
    ``table.column`` already."""
    ref = target if "." in target else f"{target}.{field}"
    return ForeignKeyConstraint([column], [ref], ondelete=ondelete, name=f"{table}_{column}_fkey", use_alter=use_alter)
