"""Merge identity-directory-search and graph-operation-id migration heads.

Revision ID: 0022_merge_graph_op_id
Revises: 0021_identity_directory_search, 0019_graph_build_operation_id
"""

revision: str = "0022_merge_graph_op_id"
down_revision: tuple[str, str] = (
    "0021_identity_directory_search",
    "0019_graph_build_operation_id",
)
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
