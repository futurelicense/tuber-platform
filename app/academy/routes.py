import logging
import secrets

from flask import render_template, request, redirect, url_for, flash, abort, session
from flask_login import login_required, login_user, current_user

from . import bp
from . import services
from .. import paystack
from ..extensions import db
from ..affiliate.tracking import record_click
from ..html_sanitize import sanitize_rich_text, video_iframe_src
from ..models import (
    User,
    AcademyCourse,
    AcademyLesson,
    AcademySettings,
    AcademySubscription,
)

logger = logging.getLogger(__name__)


def _gate_open():
    if not services.academy_is_open() and not (
        current_user.is_authenticated and current_user.role == "admin"
    ):
        abort(404)


def _resolve_ref_code():
    from_query = (request.args.get("ref") or "").strip()
    if from_query:
        code = from_query.upper()
        if session.get("ref_code") != code:
            record_click(code, "academy")
        session["ref_code"] = code
        return code
    return (session.get("ref_code") or "").strip() or None


def _expected_kobo(sub):
    return int(round(float(sub.amount) * 100))


@bp.route("/")
def home():
    _gate_open()
    _resolve_ref_code()
    if current_user.is_authenticated and current_user.role == "learner":
        sub = services.get_subscription(current_user)
        can_learn = services.user_has_course_access()
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
            if continue_course and can_learn
            else 0
        )
        progress_by_course = {}
        if can_learn:
            for c in tools + use_cases:
                progress_by_course[c.id] = services.course_progress_percent(current_user, c)
        return render_template(
            "academy/home.html",
            featured=featured,
            tools=tools,
            use_cases=use_cases,
            continue_course=continue_course,
            continue_progress=progress,
            progress_by_course=progress_by_course,
            completed_count=services.count_completed_lessons(current_user.id) if can_learn else 0,
            subscription=sub,
            can_learn=can_learn,
        )
    tools = services.published_courses(catalog="tool")[:8]
    use_cases = services.published_courses(catalog="use_case")[:8]
    settings = AcademySettings.get()
    return render_template(
        "academy/landing.html",
        tools=tools,
        use_cases=use_cases,
        settings=settings,
        ref_code=_resolve_ref_code(),
    )


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    _gate_open()
    settings = AcademySettings.get()
    ref_code = _resolve_ref_code()
    if current_user.is_authenticated:
        return redirect(url_for("academy.home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        display_name = (request.form.get("display_name") or "").strip()
        whatsapp = (request.form.get("whatsapp") or "").strip()
        entry_path = request.form.get("entry_path") or "direct_pay"
        ref_code = (request.form.get("ref_code") or ref_code or "").strip()
        try:
            ai_knowledge = int(request.form.get("ai_knowledge") or "0")
        except ValueError:
            ai_knowledge = 0

        form_ctx = dict(settings=settings, ref_code=ref_code)

        if not email or len(password) < 8:
            flash("Email and a password of at least 8 characters are required.", "error")
            return render_template("academy/signup.html", **form_ctx)
        if not whatsapp:
            flash("WhatsApp number is required.", "error")
            return render_template("academy/signup.html", **form_ctx)
        if ai_knowledge < 1 or ai_knowledge > 5:
            flash("Rate your current AI knowledge from 1 to 5.", "error")
            return render_template("academy/signup.html", **form_ctx)
        if entry_path not in ("direct_pay", "relate_admin"):
            flash("Choose how you want to enter Academy.", "error")
            return render_template("academy/signup.html", **form_ctx)
        if User.query.filter_by(email=email).first():
            flash("That email is already registered — log in instead.", "error")
            return render_template("academy/signup.html", **form_ctx)

        affiliate = services.find_affiliate(ref_code)
        user = User(email=email, role="learner", display_name=display_name or None)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        if entry_path == "relate_admin":
            sub = AcademySubscription(
                user_id=user.id,
                status="relate_only",
                entry_path="relate_admin",
                whatsapp=whatsapp,
                ai_knowledge=ai_knowledge,
                affiliate_id=affiliate.id if affiliate else None,
                referral_code_used=ref_code.upper() if affiliate else None,
            )
            db.session.add(sub)
            db.session.commit()
            login_user(user)
            services.post_message(
                user.id,
                user,
                "Hi — I chose “Relate with Admin”. Looking forward to connecting.",
            )
            flash("Account created. Message an admin below — courses unlock after they approve or you pay.", "success")
            return redirect(url_for("academy.messages"))

        # Direct pay path
        sub = AcademySubscription(
            user_id=user.id,
            status="pending",
            entry_path="direct_pay",
            whatsapp=whatsapp,
            ai_knowledge=ai_knowledge,
            affiliate_id=affiliate.id if affiliate else None,
            referral_code_used=ref_code.upper() if affiliate else None,
            amount=settings.price_amount,
            currency=settings.currency,
            paystack_reference="pending",
        )
        db.session.add(sub)
        db.session.flush()
        sub.paystack_reference = f"ac-{sub.id}-{secrets.token_hex(6)}"
        db.session.commit()
        login_user(user)

        try:
            data = paystack.initialize_transaction(
                email=email,
                amount_kobo=_expected_kobo(sub),
                reference=sub.paystack_reference,
                callback_url=url_for("academy.callback", _external=True),
                metadata={
                    "academy_subscription_id": sub.id,
                    "affiliate_id": sub.affiliate_id,
                    "user_id": user.id,
                },
            )
        except paystack.PaystackError as e:
            sub.status = "failed"
            db.session.commit()
            logger.warning("Paystack initialize failed for academy sub %s: %s", sub.id, e)
            flash("Account created, but checkout failed — try Pay again from Profile.", "error")
            return redirect(url_for("academy.profile"))

        return redirect(data["authorization_url"])

    return render_template("academy/signup.html", settings=settings, ref_code=ref_code)


@bp.route("/callback")
@login_required
def callback():
    reference = request.args.get("reference") or request.args.get("trxref")
    if not reference:
        flash("Missing payment reference.", "error")
        return redirect(url_for("academy.profile"))

    sub = AcademySubscription.query.filter_by(paystack_reference=reference).first_or_404()
    if sub.user_id != current_user.id and current_user.role != "admin":
        abort(403)

    if sub.status == "active":
        flash("Payment confirmed — welcome to Academy.", "success")
        return redirect(url_for("academy.home"))

    try:
        data = paystack.verify_transaction(reference)
    except paystack.PaystackError as e:
        logger.warning("Paystack verify failed for academy %s: %s", reference, e)
        flash("Still confirming payment — you'll get access when it clears.", "error")
        return redirect(url_for("academy.profile"))

    expected = _expected_kobo(sub)
    if data.get("status") == "success" and int(data.get("amount", -1)) == expected:
        services.mark_subscription_paid(sub)
        flash("Payment confirmed — courses unlocked.", "success")
        return redirect(url_for("academy.home"))

    flash("Payment not confirmed yet.", "error")
    return redirect(url_for("academy.profile"))


@bp.route("/pay", methods=["POST"])
@login_required
def pay_again():
    """Retry Paystack for pending/failed direct-entry learners."""
    if current_user.role != "learner":
        abort(403)
    sub = services.get_subscription(current_user)
    if sub is None:
        flash("No Academy enrollment found.", "error")
        return redirect(url_for("academy.profile"))
    if sub.status == "active":
        return redirect(url_for("academy.home"))

    settings = AcademySettings.get()
    sub.entry_path = "direct_pay"
    sub.amount = settings.price_amount
    sub.currency = settings.currency
    sub.status = "pending"
    if not sub.paystack_reference or sub.paystack_reference == "pending":
        sub.paystack_reference = f"ac-{sub.id}-{secrets.token_hex(6)}"
    else:
        # New reference so Paystack accepts a fresh initialize
        sub.paystack_reference = f"ac-{sub.id}-{secrets.token_hex(6)}"
    db.session.commit()

    try:
        data = paystack.initialize_transaction(
            email=current_user.email,
            amount_kobo=_expected_kobo(sub),
            reference=sub.paystack_reference,
            callback_url=url_for("academy.callback", _external=True),
            metadata={
                "academy_subscription_id": sub.id,
                "affiliate_id": sub.affiliate_id,
                "user_id": current_user.id,
            },
        )
    except paystack.PaystackError as e:
        sub.status = "failed"
        db.session.commit()
        logger.warning("Paystack re-init failed for academy sub %s: %s", sub.id, e)
        flash("Couldn't start checkout — try again shortly.", "error")
        return redirect(url_for("academy.profile"))

    return redirect(data["authorization_url"])


@bp.route("/courses")
def courses():
    _gate_open()
    catalog_rows = services.course_catalogs()
    valid_slugs = {c.slug for c in catalog_rows}
    catalog = (request.args.get("catalog") or "").strip() or None
    if catalog and catalog not in valid_slugs:
        catalog = None
    category = (request.args.get("category") or "").strip() or None
    listings = services.published_courses(catalog=catalog, category=category)
    categories = services.course_categories(catalog=catalog)
    progress = {}
    can_learn = services.user_has_course_access()
    featured_continue = None
    if current_user.is_authenticated and can_learn:
        for c in listings:
            progress[c.id] = services.course_progress_percent(current_user, c)
        featured = (
            AcademyCourse.query.filter_by(status="published", is_featured=True)
            .order_by(AcademyCourse.sort_order.asc())
            .first()
        )
        featured_continue = featured or (listings[0] if listings else None)
    return render_template(
        "academy/courses.html",
        courses=listings,
        categories=categories,
        catalogs=catalog_rows,
        active_catalog=catalog,
        active_category=category,
        progress=progress,
        can_learn=can_learn,
        featured_continue=featured_continue,
    )


@bp.route("/tools")
@login_required
def tools():
    _gate_open()
    from .ai import ai_configured, ai_model_label

    can_use = services.user_has_course_access()
    ai_ready = ai_configured()
    return render_template(
        "academy/tools.html",
        can_use=can_use,
        ai_ready=ai_ready,
        model_label=ai_model_label() if ai_ready else "Groq · not configured",
    )


@bp.route("/tools/chat", methods=["POST"])
@login_required
def tools_chat():
    """Groq-backed chat for Academy AI Tools (JSON)."""
    _gate_open()
    from flask import jsonify
    from .ai import (
        AIConfigError,
        AIRequestError,
        TOOL_MODES,
        build_messages,
        chat_completion,
        ai_configured,
    )

    if not services.user_has_course_access():
        return jsonify(error="Course access required for AI Tools."), 403
    if not ai_configured():
        return jsonify(error="AI_KEY is not configured on this server."), 503

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    mode = (data.get("mode") or "chat").strip().lower()
    history = data.get("history") if isinstance(data.get("history"), list) else []

    if not message:
        return jsonify(error="Type a message first."), 400
    if mode not in TOOL_MODES:
        mode = "chat"
    if len(message) > 8000:
        return jsonify(error="Message is too long (max 8,000 characters)."), 400

    # Light per-user throttle (process-local; enough to blunt spam on one worker).
    import time
    from collections import defaultdict, deque

    if not hasattr(tools_chat, "_hits"):
        tools_chat._hits = defaultdict(deque)
    hits = tools_chat._hits[current_user.id]
    now = time.monotonic()
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= 20:
        return jsonify(error="Too many requests — wait a minute and try again."), 429
    hits.append(now)

    messages = build_messages(mode, message, history)
    try:
        reply = chat_completion(messages, max_tokens=900, temperature=0.5)
    except AIConfigError as e:
        return jsonify(error=str(e)), 503
    except AIRequestError as e:
        logger.warning("Academy tools AI error for user %s: %s", current_user.id, e)
        return jsonify(error="The AI provider is busy or unavailable. Try again shortly."), 502
    except Exception:
        logger.exception("Academy tools unexpected AI failure")
        return jsonify(error="Unexpected AI error. Try again."), 500

    return jsonify(reply=reply, mode=mode)


@bp.route("/games")
def games():
    _gate_open()
    return render_template("academy/games.html")


@bp.route("/courses/<slug>")
def course_detail(slug):
    _gate_open()
    course = AcademyCourse.query.filter_by(slug=slug, status="published").first_or_404()
    can_learn = services.user_has_course_access()
    progress_by_id = services.progress_map_for_course(current_user, course) if can_learn else {}
    percent = services.course_progress_percent(current_user, course) if can_learn else 0
    nxt = services.next_lesson(course, progress_by_id) if can_learn else None
    return render_template(
        "academy/course.html",
        course=course,
        progress_by_id=progress_by_id,
        percent=percent,
        next_lesson=nxt,
        can_learn=can_learn,
        subscription=services.get_subscription(current_user) if current_user.is_authenticated else None,
    )


@bp.route("/lessons/<int:lesson_id>", methods=["GET", "POST"])
@login_required
def lesson(lesson_id):
    _gate_open()
    if not services.user_has_course_access():
        flash("Course access requires Direct Entry payment or admin approval.", "error")
        sub = services.get_subscription(current_user)
        if sub and sub.status == "relate_only":
            return redirect(url_for("academy.messages"))
        return redirect(url_for("academy.profile"))

    lesson = AcademyLesson.query.get_or_404(lesson_id)
    if not lesson.is_published or lesson.unit.course.status != "published":
        abort(404)

    course = lesson.unit.course
    if request.method == "POST":
        action = request.form.get("action")
        if action == "complete":
            services.mark_lesson_completed(current_user.id, lesson.id)
            flash("Lesson completed.", "success")
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
    interactive_choices = [
        c.strip()
        for c in (lesson.interactive_choices or "").splitlines()
        if c.strip()
    ]
    return render_template(
        "academy/lesson.html",
        course=course,
        unit=lesson.unit,
        lesson=lesson,
        progress_row=row,
        prev_lesson=prev_lesson,
        next_lesson=next_lesson,
        percent=services.course_progress_percent(current_user, course),
        content_html=sanitize_rich_text(lesson.content),
        video_embed_src=video_iframe_src(lesson.video_embed_url),
        interactive_choices=interactive_choices,
    )


@bp.route("/messages", methods=["GET", "POST"])
@login_required
def messages():
    _gate_open()
    if current_user.role not in ("learner", "admin"):
        abort(403)
    if current_user.role == "admin":
        return redirect(url_for("admin.academy_learners"))

    if not services.user_has_message_access():
        flash("Create an Academy account to message admin.", "error")
        return redirect(url_for("academy.signup"))

    sub = services.get_subscription(current_user)
    if request.method == "POST":
        body = request.form.get("body") or ""
        if services.post_message(current_user.id, current_user, body):
            flash("Message sent.", "success")
        else:
            flash("Write a message first.", "error")
        return redirect(url_for("academy.messages"))

    thread = services.thread_for_learner(current_user.id)
    services.mark_messages_read(current_user.id, "learner")
    return render_template(
        "academy/messages.html",
        thread=thread,
        subscription=sub,
        can_learn=services.user_has_course_access(),
        settings=AcademySettings.get(),
        poll_url=url_for("academy.messages_updates"),
        send_url=url_for("academy.messages_send"),
        me_role="learner",
    )


@bp.route("/messages/updates")
@login_required
def messages_updates():
    _gate_open()
    if current_user.role != "learner" or not services.user_has_message_access():
        abort(403)
    after_id = request.args.get("after_id", 0, type=int) or 0
    rows = services.messages_after(current_user.id, after_id)
    if rows:
        services.mark_messages_read(current_user.id, "learner")
    return {
        "messages": [services.serialize_message(m) for m in rows],
        "unread": services.unread_from_admin(current_user.id),
    }


@bp.route("/messages/send", methods=["POST"])
@login_required
def messages_send():
    _gate_open()
    if current_user.role != "learner" or not services.user_has_message_access():
        abort(403)
    data = request.get_json(silent=True) or {}
    body = data.get("body") if data else request.form.get("body")
    msg = services.post_message(current_user.id, current_user, body or "")
    if not msg:
        return {"error": "Write a message first."}, 400
    return {"message": services.serialize_message(msg)}


@bp.route("/profile")
@login_required
def profile():
    _gate_open()
    if current_user.role not in ("learner", "admin"):
        return redirect(url_for("academy.home"))
    can_learn = services.user_has_course_access()
    courses = services.published_courses() if can_learn else []
    progress = {c.id: services.course_progress_percent(current_user, c) for c in courses}
    sub = services.get_subscription(current_user)
    return render_template(
        "academy/profile.html",
        courses=courses,
        progress=progress,
        subscription=sub,
        can_learn=can_learn,
        completed_count=services.count_completed_lessons(current_user.id) if can_learn else 0,
        settings=AcademySettings.get(),
    )
