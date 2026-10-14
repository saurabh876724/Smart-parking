"""the logout epoch and the `logout` audit action (Phase 10)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12 11:40:00.000000

Written by hand, then checked by `test_the_migrated_schema_matches_the_models_exactly`
and `test_the_migrations_are_reversible`, which diff this revision's result against
the models in both directions.

Two changes, and the second is the reason the revision exists at all:

* ``users.tokens_invalid_before`` - nullable DATETIME. `POST /auth/logout` can only
  mean something durable if the server keeps a cutoff, because a JWT carries no
  server-side state to strike it off. NULL for an account that has never signed
  out, so a first logout withdraws everything issued before it.
* ``audit_logs`` rebuilt so its ``action`` CHECK accepts ``logout``. SQLite cannot
  alter a CHECK in place, hence ``batch_alter_table``: create the new table, copy
  every existing row, swap the names. **The audit rows already in
  `data/smart_parking.db` are what that care is for** - a rebuild that lost them
  would leave a table with the right shape and no history, which is the worst way
  for an append-only trail to fail. The constraint keeps its ``0002`` name, so the
  diff against the models stays an equality rather than becoming a rename.

The action list is `db/models.py::AUDIT_ACTIONS` reproduced literally; if the two
disagree, `POST /auth/logout` raises IntegrityError on a migrated database while
working on one built by ``create_all`` - which is the drift a test below guards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from smart_parking.db.base import NAMING_CONVENTION


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The vocabulary this revision moves to - models.AUDIT_ACTIONS with `logout` in it.
_ACTIONS = (
    "login",
    "login_failed",
    "logout",
    "user_created",
    "user_deactivated",
    "plate_corrected",
    "session_force_closed",
    "camera_registered",
    "camera_updated",
    "camera_restart",
    "slot_created",
    "slot_polygon_updated",
)
# The vocabulary `0002` created the table with.
_PREVIOUS_ACTIONS = tuple(action for action in _ACTIONS if action != "logout")

_CHECK_NAME = "action_is_a_written_operation"


def _rebuild(acceptable: tuple[str, ...]) -> None:
    """Swap the CHECK, carrying the rows and the rest of the table over.

    Both names are the *short* one: `batch_alter_table` renders a constraint
    through the metadata's naming convention, so asking it to drop
    `ck_audit_logs_action_is_a_written_operation` literally would look for a
    constraint called `ck_audit_logs_ck_audit_logs_...`.
    """
    with op.batch_alter_table("audit_logs", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(_CHECK_NAME, type_="check")
        batch.create_check_constraint(_CHECK_NAME, f"action IN {acceptable!r}")


def upgrade() -> None:
    op.add_column("users", sa.Column("tokens_invalid_before", sa.DateTime(), nullable=True))
    _rebuild(_ACTIONS)


def downgrade() -> None:
    _rebuild(_PREVIOUS_ACTIONS)
    op.drop_column("users", "tokens_invalid_before")
