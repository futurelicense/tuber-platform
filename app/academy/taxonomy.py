"""Academy catalog / category taxonomy helpers (admin-managed, DB-backed)."""

from __future__ import annotations

import re

from ..extensions import db
from ..models.academy import (
    COURSE_CATALOGS,
    COURSE_CATEGORIES_BY_CATALOG,
    AcademyCatalog,
    AcademyCategory,
)


_DEFAULT_LABELS = {
    "tool": "AI tools",
    "use_case": "Use cases",
    "challenge": "Challenges",
}


def _slugify_catalog(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return slug or "catalog"


def seed_default_taxonomy(*, force: bool = False) -> int:
    """Insert default catalogs/categories when the tables are empty.

    Returns number of catalogs created. With force=True, missing defaults are
    added even if other catalogs already exist.
    """
    created = 0
    existing = {c.slug: c for c in AcademyCatalog.query.all()}
    if existing and not force:
        return 0

    for i, slug in enumerate(COURSE_CATALOGS):
        catalog = existing.get(slug)
        if catalog is None:
            catalog = AcademyCatalog(
                slug=slug,
                label=_DEFAULT_LABELS.get(slug, slug.replace("_", " ").title()),
                sort_order=i * 10,
                is_active=True,
            )
            db.session.add(catalog)
            db.session.flush()
            existing[slug] = catalog
            created += 1
        known = {c.name for c in catalog.categories}
        for j, name in enumerate(COURSE_CATEGORIES_BY_CATALOG.get(slug) or ()):
            if name in known:
                continue
            db.session.add(
                AcademyCategory(
                    catalog_id=catalog.id,
                    name=name,
                    sort_order=j * 10,
                    is_active=True,
                )
            )
            known.add(name)
    db.session.commit()
    return created


def ensure_taxonomy():
    """Guarantee at least the default catalogs exist (idempotent)."""
    if AcademyCatalog.query.count() == 0:
        seed_default_taxonomy()


def active_catalogs():
    ensure_taxonomy()
    return (
        AcademyCatalog.query.filter_by(is_active=True)
        .order_by(AcademyCatalog.sort_order.asc(), AcademyCatalog.label.asc())
        .all()
    )


def all_catalogs():
    ensure_taxonomy()
    return AcademyCatalog.query.order_by(
        AcademyCatalog.sort_order.asc(), AcademyCatalog.label.asc()
    ).all()


def catalog_slugs(active_only: bool = True):
    rows = active_catalogs() if active_only else all_catalogs()
    return tuple(c.slug for c in rows)


def categories_by_catalog_map(active_only: bool = True):
    """Dict slug -> list of category names for course forms / filters."""
    ensure_taxonomy()
    out = {}
    q = AcademyCatalog.query.order_by(AcademyCatalog.sort_order.asc())
    for catalog in q.all():
        cats = catalog.categories
        if active_only:
            cats = [c for c in cats if c.is_active]
        else:
            cats = list(cats)
        cats = sorted(cats, key=lambda c: (c.sort_order, c.name.lower()))
        out[catalog.slug] = [c.name for c in cats]
    return out


def category_names(catalog_slug=None, active_only: bool = True):
    mapping = categories_by_catalog_map(active_only=active_only)
    if catalog_slug:
        return list(mapping.get(catalog_slug) or [])
    names = []
    seen = set()
    for cats in mapping.values():
        for name in cats:
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def validate_catalog_slug(slug: str) -> bool:
    if not slug:
        return False
    return AcademyCatalog.query.filter_by(slug=slug, is_active=True).first() is not None


def validate_category(catalog_slug: str, category: str | None) -> bool:
    if not category:
        return True
    allowed = set(category_names(catalog_slug, active_only=True))
    # Also allow any active category name globally (legacy courses may cross-map).
    if not allowed:
        allowed = set(category_names(active_only=True))
    return category in allowed


def create_catalog(slug: str, label: str, sort_order: int = 0) -> tuple[AcademyCatalog | None, str | None]:
    slug = _slugify_catalog(slug)
    label = (label or "").strip() or slug.replace("_", " ").title()
    if not slug:
        return None, "Catalog slug is required."
    if AcademyCatalog.query.filter_by(slug=slug).first():
        return None, f"Catalog '{slug}' already exists."
    row = AcademyCatalog(slug=slug, label=label, sort_order=sort_order or 0, is_active=True)
    db.session.add(row)
    db.session.commit()
    return row, None


def update_catalog(
    catalog_id: int, *, label: str | None = None, sort_order: int | None = None, is_active: bool | None = None
) -> tuple[AcademyCatalog | None, str | None]:
    row = AcademyCatalog.query.get(catalog_id)
    if row is None:
        return None, "Catalog not found."
    if label is not None:
        label = label.strip()
        if not label:
            return None, "Label is required."
        row.label = label
    if sort_order is not None:
        row.sort_order = sort_order
    if is_active is not None:
        row.is_active = is_active
    db.session.commit()
    return row, None


def delete_catalog(catalog_id: int) -> str | None:
    from ..models import AcademyCourse

    row = AcademyCatalog.query.get(catalog_id)
    if row is None:
        return "Catalog not found."
    in_use = AcademyCourse.query.filter_by(catalog=row.slug).count()
    if in_use:
        return f"Cannot delete — {in_use} course(s) still use this catalog. Archive or reassign them first."
    db.session.delete(row)
    db.session.commit()
    return None


def create_category(
    catalog_id: int, name: str, sort_order: int = 0
) -> tuple[AcademyCategory | None, str | None]:
    catalog = AcademyCatalog.query.get(catalog_id)
    if catalog is None:
        return None, "Catalog not found."
    name = (name or "").strip()
    if not name:
        return None, "Category name is required."
    if AcademyCategory.query.filter_by(catalog_id=catalog_id, name=name).first():
        return None, f"Category '{name}' already exists in this catalog."
    row = AcademyCategory(
        catalog_id=catalog_id, name=name, sort_order=sort_order or 0, is_active=True
    )
    db.session.add(row)
    db.session.commit()
    return row, None


def update_category(
    category_id: int,
    *,
    name: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> tuple[AcademyCategory | None, str | None]:
    from ..models import AcademyCourse

    row = AcademyCategory.query.get(category_id)
    if row is None:
        return None, "Category not found."
    old_name = row.name
    if name is not None:
        name = name.strip()
        if not name:
            return None, "Category name is required."
        clash = (
            AcademyCategory.query.filter_by(catalog_id=row.catalog_id, name=name)
            .filter(AcademyCategory.id != row.id)
            .first()
        )
        if clash:
            return None, f"Category '{name}' already exists in this catalog."
        row.name = name
    if sort_order is not None:
        row.sort_order = sort_order
    if is_active is not None:
        row.is_active = is_active
    db.session.flush()
    if name is not None and name != old_name:
        AcademyCourse.query.filter_by(catalog=row.catalog.slug, category=old_name).update(
            {"category": name}, synchronize_session=False
        )
    db.session.commit()
    return row, None


def delete_category(category_id: int) -> str | None:
    from ..models import AcademyCourse

    row = AcademyCategory.query.get(category_id)
    if row is None:
        return "Category not found."
    in_use = AcademyCourse.query.filter_by(
        catalog=row.catalog.slug, category=row.name
    ).count()
    if in_use:
        return f"Cannot delete — {in_use} course(s) still use this category. Reassign them first."
    db.session.delete(row)
    db.session.commit()
    return None
