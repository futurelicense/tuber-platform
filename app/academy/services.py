from datetime import datetime, timezone

from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    AcademyCourse,
    AcademyLesson,
    AcademyLessonProgress,
    AcademyMessage,
    AcademySettings,
    AcademySubscription,
    Commission,
    User,
    effective_commission_rate,
)


def published_courses(catalog=None, category=None):
    q = AcademyCourse.query.filter_by(status="published")
    if catalog:
        q = q.filter_by(catalog=catalog)
    if category:
        q = q.filter_by(category=category)
    return q.order_by(AcademyCourse.sort_order.asc(), AcademyCourse.created_at.desc()).all()


def course_categories(catalog=None):
    """Active category pills for a catalog (admin-managed taxonomy)."""
    from . import taxonomy as academy_taxonomy

    return academy_taxonomy.category_names(catalog_slug=catalog, active_only=True)


def course_catalogs():
    """Active catalogs for learner filters."""
    from . import taxonomy as academy_taxonomy

    return academy_taxonomy.active_catalogs()


def academy_is_open():
    return bool(AcademySettings.get().is_open)


def find_affiliate(code):
    if not code:
        return None
    return User.query.filter_by(
        referral_code=code.upper(), role="affiliate", is_active_flag=True
    ).first()


def get_subscription(user):
    if user is None:
        return None
    return AcademySubscription.query.filter_by(user_id=user.id).first()


def user_has_course_access(user=None):
    """Paid direct-entry learners + admins/staff preview."""
    user = user or (current_user if current_user.is_authenticated else None)
    if user is None:
        return False
    if user.role == "admin":
        return True
    if user.role in ("clipper", "producer", "affiliate"):
        return True
    if user.role != "learner":
        return False
    sub = get_subscription(user)
    return bool(sub and sub.status == "active")


def user_has_message_access(user=None):
    user = user or (current_user if current_user.is_authenticated else None)
    if user is None:
        return False
    if user.role == "admin":
        return True
    if user.role != "learner":
        return False
    sub = get_subscription(user)
    return bool(sub and sub.status in ("relate_only", "active", "pending"))


# Back-compat name used by templates/routes
user_has_academy_access = user_has_course_access


def lesson_list_for_course(course):
    lessons = []
    for unit in course.units:
        for lesson in unit.lessons:
            if lesson.is_published:
                lessons.append(lesson)
    return lessons


def course_progress_percent(user, course):
    lessons = lesson_list_for_course(course)
    if not lessons or user is None or not getattr(user, "is_authenticated", False):
        return 0
    ids = [l.id for l in lessons]
    done = (
        AcademyLessonProgress.query.filter(
            AcademyLessonProgress.user_id == user.id,
            AcademyLessonProgress.lesson_id.in_(ids),
            AcademyLessonProgress.status == "completed",
        ).count()
    )
    return int(round(100 * done / len(lessons)))


def get_or_create_progress(user_id, lesson_id):
    row = AcademyLessonProgress.query.filter_by(user_id=user_id, lesson_id=lesson_id).first()
    if row is None:
        row = AcademyLessonProgress(
            user_id=user_id,
            lesson_id=lesson_id,
            status="not_started",
            progress_percent=0,
        )
        db.session.add(row)
        db.session.flush()
    return row


def mark_lesson_started(user_id, lesson_id):
    row = get_or_create_progress(user_id, lesson_id)
    if row.status == "not_started":
        row.status = "in_progress"
        row.progress_percent = max(row.progress_percent, 10)
        db.session.commit()
    return row


def mark_lesson_completed(user_id, lesson_id):
    row = get_or_create_progress(user_id, lesson_id)
    row.status = "completed"
    row.progress_percent = 100
    row.completed_at = datetime.now(timezone.utc)
    db.session.commit()
    return row


def progress_map_for_course(user, course):
    if user is None or not getattr(user, "is_authenticated", False):
        return {}
    lessons = lesson_list_for_course(course)
    if not lessons:
        return {}
    ids = [l.id for l in lessons]
    rows = AcademyLessonProgress.query.filter(
        AcademyLessonProgress.user_id == user.id,
        AcademyLessonProgress.lesson_id.in_(ids),
    ).all()
    return {r.lesson_id: r for r in rows}


def next_lesson(course, progress_by_id):
    for lesson in lesson_list_for_course(course):
        row = progress_by_id.get(lesson.id)
        if row is None or row.status != "completed":
            return lesson
    lessons = lesson_list_for_course(course)
    return lessons[-1] if lessons else None


def count_completed_lessons(user_id):
    return AcademyLessonProgress.query.filter_by(
        user_id=user_id, status="completed"
    ).count()


def mark_subscription_paid(sub):
    """Idempotent paid marker + affiliate commission (reference prefix ac-)."""
    if sub.status == "active":
        return

    sub.status = "active"
    sub.paid_at = datetime.now(timezone.utc)

    if sub.affiliate_id is not None and sub.amount is not None:
        rate = effective_commission_rate(sub.affiliate)
        db.session.add(
            Commission(
                affiliate_id=sub.affiliate_id,
                amount=sub.amount * rate / 100,
                rate_percent_snapshot=rate,
                note=f"Auto: Academy direct entry #{sub.id}",
                created_by_id=None,
                source_academy_subscription_id=sub.id,
            )
        )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def post_message(learner_id, sender, body):
    body = (body or "").strip()
    if not body:
        return None
    if len(body) > 4000:
        body = body[:4000]
    role = "admin" if sender.role == "admin" else "learner"
    msg = AcademyMessage(
        learner_id=learner_id,
        sender_role=role,
        sender_id=sender.id,
        body=body,
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def thread_for_learner(learner_id):
    return (
        AcademyMessage.query.filter_by(learner_id=learner_id)
        .order_by(AcademyMessage.created_at.asc())
        .all()
    )


def messages_after(learner_id, after_id=0):
    q = AcademyMessage.query.filter_by(learner_id=learner_id)
    if after_id:
        q = q.filter(AcademyMessage.id > int(after_id))
    return q.order_by(AcademyMessage.id.asc()).limit(100).all()


def serialize_message(msg):
    created = msg.created_at
    return {
        "id": msg.id,
        "sender_role": msg.sender_role,
        "body": msg.body,
        "created_at": created.isoformat() if created else None,
        "created_label": created.strftime("%b %d %H:%M") if created else "",
    }


def mark_messages_read(learner_id, viewer_role):
    """Mark the other party's unread messages as read for this thread."""
    other = "admin" if viewer_role == "learner" else "learner"
    now = datetime.now(timezone.utc)
    (
        AcademyMessage.query.filter_by(learner_id=learner_id, sender_role=other)
        .filter(AcademyMessage.read_at.is_(None))
        .update({"read_at": now}, synchronize_session=False)
    )
    db.session.commit()


def unread_from_learners(learner_id=None):
    """Unread learner→admin messages (for admin badges)."""
    q = AcademyMessage.query.filter_by(sender_role="learner").filter(
        AcademyMessage.read_at.is_(None)
    )
    if learner_id is not None:
        q = q.filter_by(learner_id=learner_id)
    return q.count()


def unread_from_admin(learner_id):
    """Unread admin→learner messages for one learner."""
    return (
        AcademyMessage.query.filter_by(learner_id=learner_id, sender_role="admin")
        .filter(AcademyMessage.read_at.is_(None))
        .count()
    )


def grant_course_access(sub):
    """Admin upgrades a relate-with-admin learner to full course access."""
    sub.status = "active"
    sub.entry_path = sub.entry_path or "relate_admin"
    if sub.paid_at is None:
        sub.paid_at = datetime.now(timezone.utc)
    db.session.commit()
    return sub
