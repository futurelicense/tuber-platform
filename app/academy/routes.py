from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, login_user, current_user

from . import bp
from . import services
from ..extensions import db
from ..models import (
    User,
    AcademyCourse,
    AcademyLesson,
    AcademySettings,
    AcademySubscription,
)


def _gate_open():
    if not services.academy_is_open() and not (
        current_user.is_authenticated and current_user.role == "admin"
    ):
        abort(404)


@bp.route("/")
def home():
    _gate_open()
    if current_user.is_authenticated and current_user.role == "learner":
        featured = (
            AcademyCourse.query.filter_by(status="published", is_featured=True)
            .order_by(AcademyCourse.sort_order.asc())
            .limit(6)
            .all()
        )
        tools = services.published_courses(catalog="tool")[:6]
        use_cases = services.published_courses(catalog="use_case")[:6]
        continue_course = featured[0] if featured else (tools[0] if tools else None)
        progress = (
            services.course_progress_percent(current_user, continue_course)
            if continue_course
            else 0
        )
        return render_template(
            "academy/home.html",
            featured=featured,
            tools=tools,
            use_cases=use_cases,
            continue_course=continue_course,
            continue_progress=progress,
            completed_count=services.count_completed_lessons(current_user.id),
        )
    # Marketing landing for guests / other roles
    tools = services.published_courses(catalog="tool")[:8]
    use_cases = services.published_courses(catalog="use_case")[:8]
    settings = AcademySettings.get()
    return render_template(
        "academy/landing.html",
        tools=tools,
        use_cases=use_cases,
        settings=settings,
    )


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    _gate_open()
    if current_user.is_authenticated:
        return redirect(url_for("academy.home"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        display_name = (request.form.get("display_name") or "").strip()
        if not email or len(password) < 8:
            flash("Email and a password of at least 8 characters are required.", "error")
            return render_template("academy/signup.html")
        if User.query.filter_by(email=email).first():
            flash("That email is already registered — log in instead.", "error")
            return render_template("academy/signup.html")
        user = User(email=email, role="learner", display_name=display_name or None)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        services.ensure_learner_subscription(user)
        db.session.commit()
        login_user(user)
        flash("Welcome to MoneyTuber Academy.", "success")
        return redirect(url_for("academy.home"))
    return render_template("academy/signup.html")


@bp.route("/courses")
def courses():
    _gate_open()
    catalog = (request.args.get("catalog") or "").strip() or None
    if catalog and catalog not in ("tool", "use_case", "challenge"):
        catalog = None
    category = (request.args.get("category") or "").strip() or None
    listings = services.published_courses(catalog=catalog, category=category)
    categories = services.course_categories(catalog=catalog)
    progress = {}
    if current_user.is_authenticated:
        for c in listings:
            progress[c.id] = services.course_progress_percent(current_user, c)
    return render_template(
        "academy/courses.html",
        courses=listings,
        categories=categories,
        active_catalog=catalog,
        active_category=category,
        progress=progress,
    )


@bp.route("/courses/<slug>")
def course_detail(slug):
    _gate_open()
    course = AcademyCourse.query.filter_by(slug=slug, status="published").first_or_404()
    progress_by_id = services.progress_map_for_course(current_user, course)
    percent = services.course_progress_percent(current_user, course)
    nxt = services.next_lesson(course, progress_by_id)
    return render_template(
        "academy/course.html",
        course=course,
        progress_by_id=progress_by_id,
        percent=percent,
        next_lesson=nxt,
        can_learn=services.user_has_academy_access(),
    )


@bp.route("/lessons/<int:lesson_id>", methods=["GET", "POST"])
@login_required
def lesson(lesson_id):
    _gate_open()
    if not services.user_has_academy_access():
        flash("Start a free Academy account to take lessons.", "error")
        return redirect(url_for("academy.signup"))

    lesson = AcademyLesson.query.get_or_404(lesson_id)
    if not lesson.is_published or lesson.unit.course.status != "published":
        abort(404)

    course = lesson.unit.course
    if request.method == "POST":
        action = request.form.get("action")
        if action == "complete":
            services.mark_lesson_completed(current_user.id, lesson.id)
            flash("Lesson completed.", "success")
            # Advance to next incomplete lesson when possible
            progress_by_id = services.progress_map_for_course(current_user, course)
            nxt = services.next_lesson(course, progress_by_id)
            if nxt and nxt.id != lesson.id:
                return redirect(url_for("academy.lesson", lesson_id=nxt.id))
            return redirect(url_for("academy.course_detail", slug=course.slug))
        return redirect(url_for("academy.lesson", lesson_id=lesson.id))

    services.mark_lesson_started(current_user.id, lesson.id)
    progress_by_id = services.progress_map_for_course(current_user, course)
    lessons = services.lesson_list_for_course(course)
    idx = next((i for i, l in enumerate(lessons) if l.id == lesson.id), 0)
    prev_lesson = lessons[idx - 1] if idx > 0 else None
    next_lesson = lessons[idx + 1] if idx + 1 < len(lessons) else None
    row = progress_by_id.get(lesson.id)
    return render_template(
        "academy/lesson.html",
        course=course,
        unit=lesson.unit,
        lesson=lesson,
        progress_row=row,
        prev_lesson=prev_lesson,
        next_lesson=next_lesson,
        percent=services.course_progress_percent(current_user, course),
    )


@bp.route("/profile")
@login_required
def profile():
    _gate_open()
    if current_user.role not in ("learner", "admin"):
        return redirect(url_for("academy.home"))
    courses = services.published_courses()
    progress = {c.id: services.course_progress_percent(current_user, c) for c in courses}
    sub = AcademySubscription.query.filter_by(user_id=current_user.id).first()
    return render_template(
        "academy/profile.html",
        courses=courses,
        progress=progress,
        subscription=sub,
        completed_count=services.count_completed_lessons(current_user.id),
    )
