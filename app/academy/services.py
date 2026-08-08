from datetime import datetime, timezone

from flask_login import current_user

from ..extensions import db
from ..models import (
    AcademyCourse,
    AcademyLesson,
    AcademyLessonProgress,
    AcademySettings,
    AcademySubscription,
    AcademyUnit,
)


def published_courses(catalog=None, category=None):
    q = AcademyCourse.query.filter_by(status="published")
    if catalog:
        q = q.filter_by(catalog=catalog)
    if category:
        q = q.filter_by(category=category)
    return q.order_by(AcademyCourse.sort_order.asc(), AcademyCourse.created_at.desc()).all()


def course_categories(catalog=None):
    q = (
        db.session.query(AcademyCourse.category)
        .filter(AcademyCourse.status == "published", AcademyCourse.category.isnot(None))
        .filter(AcademyCourse.category != "")
    )
    if catalog:
        q = q.filter(AcademyCourse.catalog == catalog)
    rows = q.distinct().order_by(AcademyCourse.category.asc()).all()
    return [r[0] for r in rows]


def academy_is_open():
    return bool(AcademySettings.get().is_open)


def user_has_academy_access(user=None):
    """Admins always; learners when Academy is open and (no sub required or active sub)."""
    user = user or (current_user if current_user.is_authenticated else None)
    settings = AcademySettings.get()
    if not settings.is_open:
        return bool(user and user.role == "admin")
    if user is None:
        return False
    if user.role == "admin":
        return True
    if user.role != "learner":
        # Staff can preview published content while logged in.
        return user.role in ("clipper", "producer", "affiliate")
    if not settings.require_subscription:
        return True
    sub = AcademySubscription.query.filter_by(user_id=user.id).first()
    return bool(sub and sub.status in ("trialing", "active"))


def lesson_list_for_course(course):
    lessons = []
    for unit in course.units:
        for lesson in unit.lessons:
            if lesson.is_published:
                lessons.append(lesson)
    return lessons


def course_progress_percent(user, course):
    lessons = lesson_list_for_course(course)
    if not lessons or user is None or not user.is_authenticated:
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


def ensure_learner_subscription(user):
    """When subscription is not required, still create an active row for bookkeeping."""
    sub = AcademySubscription.query.filter_by(user_id=user.id).first()
    if sub is None:
        sub = AcademySubscription(user_id=user.id, status="active")
        db.session.add(sub)
        db.session.commit()
    return sub


def count_completed_lessons(user_id):
    return AcademyLessonProgress.query.filter_by(
        user_id=user_id, status="completed"
    ).count()
