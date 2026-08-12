"""academy catalogs and categories taxonomy

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-12 04:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_DEFAULT_CATALOGS = (
    ("tool", "AI tools", 0),
    ("use_case", "Use cases", 10),
    ("challenge", "Challenges", 20),
)
_DEFAULT_CATEGORIES = {
    "tool": (
        "New",
        "Research & Analysis",
        "No-Code Apps",
        "Writing",
        "Business",
        "Operations",
        "Image Generation",
    ),
    "use_case": (
        "New",
        "Business",
        "Operations",
        "Marketing and Growth",
        "Self-improvement",
        "Creation",
    ),
    "challenge": (
        "New",
        "Beginner",
        "Intermediate",
        "Certificate",
    ),
}


def upgrade():
    op.create_table(
        "academy_catalogs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_academy_catalogs_slug", "academy_catalogs", ["slug"], unique=True)

    op.create_table(
        "academy_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("catalog_id", sa.Integer(), sa.ForeignKey("academy_catalogs.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("catalog_id", "name", name="uq_academy_category_catalog_name"),
    )
    op.create_index("ix_academy_categories_catalog_id", "academy_categories", ["catalog_id"])

    bind = op.get_bind()
    with op.batch_alter_table("academy_courses", schema=None) as batch_op:
        batch_op.alter_column(
            "catalog",
            existing_type=sa.String(length=20),
            type_=sa.String(length=50),
            existing_nullable=False,
        )
        if bind.dialect.name != "sqlite":
            batch_op.drop_constraint("ck_academy_course_catalog", type_="check")

    catalogs = sa.table(
        "academy_catalogs",
        sa.column("id", sa.Integer),
        sa.column("slug", sa.String),
        sa.column("label", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )
    categories = sa.table(
        "academy_categories",
        sa.column("catalog_id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )

    for slug, label, sort_order in _DEFAULT_CATALOGS:
        bind.execute(
            catalogs.insert().values(
                slug=slug, label=label, sort_order=sort_order, is_active=True
            )
        )
    rows = bind.execute(sa.select(catalogs.c.id, catalogs.c.slug)).fetchall()
    id_by_slug = {slug: cid for cid, slug in rows}
    for slug, names in _DEFAULT_CATEGORIES.items():
        cid = id_by_slug.get(slug)
        if not cid:
            continue
        for i, name in enumerate(names):
            bind.execute(
                categories.insert().values(
                    catalog_id=cid, name=name, sort_order=i * 10, is_active=True
                )
            )


def downgrade():
    op.drop_index("ix_academy_categories_catalog_id", table_name="academy_categories")
    op.drop_table("academy_categories")
    op.drop_index("ix_academy_catalogs_slug", table_name="academy_catalogs")
    op.drop_table("academy_catalogs")
    with op.batch_alter_table("academy_courses", schema=None) as batch_op:
        batch_op.alter_column(
            "catalog",
            existing_type=sa.String(length=50),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_academy_course_catalog",
            "catalog in ('tool','use_case','challenge')",
        )
