"""Idempotent sample curriculum for MoneyTuber Academy demos and homepage.

Run:  flask seed-academy-sample
Force recreate:  flask seed-academy-sample --force
"""

SAMPLE_SLUG = "chatgpt-for-youtube-creators"

COURSE = {
    "title": "ChatGPT for YouTube Creators",
    "slug": SAMPLE_SLUG,
    "summary": "Write hooks, scripts, and titles with ChatGPT — built for faceless and on-camera channels.",
    "description": (
        "A practical starter course for MoneyTuber operators. Learn how to brief ChatGPT, "
        "turn ideas into Shorts and long-form scripts, and build a repeatable research → draft → polish workflow. "
        "No engineering required — just clear prompts and creator judgment."
    ),
    "catalog": "tool",
    "category": "Writing",
    "tags": "ChatGPT, Scripts, Hooks, Titles, Faceless",
    "estimated_lessons": 9,
    "estimated_hours": 1.5,
    "status": "published",
    "is_featured": True,
    "sort_order": 0,
}

# Each unit: title, optional section_label, lessons[]
# Lesson keys match AcademyLesson fields (content is rich HTML).
UNITS = [
    {
        "title": "UNIT 1 Meet ChatGPT",
        "section_label": None,
        "lessons": [
            {
                "title": "What ChatGPT is (and isn't) for creators",
                "lesson_type": "read",
                "estimated_minutes": 6,
                "content": """
<p>ChatGPT is a <strong>drafting partner</strong>, not a replacement for your taste or niche judgment.</p>
<p>Use it to:</p>
<ul>
  <li>Expand a rough idea into outlines and first drafts</li>
  <li>Generate hook variants and title options fast</li>
  <li>Rewrite for clarity, length, or platform (Shorts vs long-form)</li>
</ul>
<p>Do <em>not</em> trust it blindly for:</p>
<ul>
  <li>Facts, stats, or legal claims — verify before you publish</li>
  <li>Your unique voice — always edit the last 10%</li>
  <li>Thumbnail concepts that ignore your brand colors</li>
</ul>
<p><strong>Try today:</strong> Open ChatGPT and paste one recent video idea in one sentence. Ask: <em>“Give me 5 angles for a YouTube Short under 45 seconds.”</em> Pick the angle you’d actually film.</p>
""",
            },
            {
                "title": "How to brief ChatGPT like an operator",
                "lesson_type": "listen",
                "estimated_minutes": 8,
                "content": """
<p><strong>Listen / read-along script</strong></p>
<p>Great ChatGPT output starts with a great brief. Think of four ingredients:</p>
<ol>
  <li><strong>Role</strong> — who should it act as? (YouTube scriptwriter for faceless finance)</li>
  <li><strong>Audience</strong> — who watches? (busy adults, 25–40, hate fluff)</li>
  <li><strong>Goal</strong> — what should the viewer do? (watch to the end / click next)</li>
  <li><strong>Constraints</strong> — length, tone, banned words, format</li>
</ol>
<p>Weak brief: <em>“Write a YouTube script about money.”</em></p>
<p>Strong brief: <em>“Act as a YouTube Shorts writer. Audience: beginners scared of investing. Goal: one clear tip in 40 seconds. Tone: calm, no hype. Format: hook → tip → CTA. Avoid saying ‘financial advice’.”</em></p>
<p>When the draft is mediocre, don’t restart from zero — reply with surgery: <em>“Shorter hook. More concrete example. Cut the intro.”</em></p>
""",
            },
            {
                "title": "Interactive: build your creator brief",
                "lesson_type": "interactive",
                "estimated_minutes": 7,
                "content": """
<p>Fill the template using the chips, then paste your finished brief into ChatGPT and generate 3 Shorts angles.</p>
""",
                "interactive_instruction": "Build a one-paragraph ChatGPT brief for your next video.",
                "interactive_template": "Act as a [role] for YouTube. Audience: [audience]. Goal: [goal]. Topic: [topic]. Constraints: [constraints].",
                "interactive_choices": "\n".join(
                    [
                        "Shorts scriptwriter",
                        "long-form narrator writer",
                        "busy beginners",
                        "ambitious creators",
                        "watch to the end",
                        "click the next video",
                        "under 45 seconds",
                        "calm, no hype",
                        "no jargon",
                    ]
                ),
                "interactive_check_tip": "Good briefs name role + audience + goal + at least one constraint. If ChatGPT still sounds generic, add a concrete example from your niche.",
            },
        ],
    },
    {
        "title": "UNIT 2 Write Faster",
        "section_label": "Scripts, hooks & titles",
        "lessons": [
            {
                "title": "Hook formulas that stop the scroll",
                "lesson_type": "read",
                "estimated_minutes": 8,
                "content": """
<p>Your first <strong>1–2 seconds</strong> decide retention. ChatGPT is excellent at generating options — you pick the winner.</p>
<p>Four formulas that work for faceless and talking-head:</p>
<ol>
  <li><strong>Curiosity gap</strong> — “Most creators waste money on this one thumbnail mistake…”</li>
  <li><strong>Specific number</strong> — “I cut my edit time from 4 hours to 40 minutes.”</li>
  <li><strong>Pattern interrupt</strong> — unexpected claim, then prove it</li>
  <li><strong>Viewer identity</strong> — “If you post Shorts but get zero saves…”</li>
</ol>
<p><strong>Prompt to steal:</strong></p>
<p><em>Write 12 YouTube Short hooks for [topic]. Mix curiosity, numbers, and identity. Max 14 words each. No clickbait that we can’t deliver in 40 seconds.</em></p>
<p>Then ask: <em>Rank the top 3 for retention and explain why in one line each.</em></p>
""",
            },
            {
                "title": "From outline to Shorts script",
                "lesson_type": "video",
                "estimated_minutes": 10,
                # Optional: set in Admin → Edit lesson → Video URL after you pick a walkthrough.
                "video_embed_url": None,
                "content": """
<p><strong>Video lesson notes</strong> — add your preferred ChatGPT walkthrough URL in Admin (YouTube/Vimeo). Until then, follow the outline below in the tool.</p>
<p>Shorts script skeleton:</p>
<ul>
  <li>Hook (0–3s)</li>
  <li>One idea (3–30s)</li>
  <li>Proof or example (quick)</li>
  <li>CTA (save / follow / next)</li>
</ul>
<p><strong>Prompt:</strong> <em>Using my brief, write a 40-second Shorts script with spoken lines only (no stage directions longer than 3 words). Mark [HOOK] [BODY] [CTA].</em></p>
<p>Read it aloud once — if you stumble, simplify the sentences and regenerate.</p>
""",
            },
            {
                "title": "Interactive: title & thumbnail text pack",
                "lesson_type": "interactive",
                "estimated_minutes": 8,
                "content": """
<p>Titles and on-thumbnail text should promise the same outcome. Use ChatGPT for volume, you for truth.</p>
""",
                "interactive_instruction": "Complete the pack prompt, then run it in ChatGPT for your next upload.",
                "interactive_template": "Give me 10 YouTube titles and matching 3–4 word thumbnail texts for a video about [topic] aimed at [audience]. Titles under 60 characters. No all-caps. Avoid words: [banned].",
                "interactive_choices": "\n".join(
                    [
                        "faceless channel growth",
                        "ChatGPT scripting",
                        "beginner creators",
                        "channel buyers",
                        "guaranteed",
                        "secret",
                        "make money overnight",
                    ]
                ),
                "interactive_check_tip": "You should get pairs where the thumbnail text is shorter and punchier than the title — not a duplicate of the full title.",
            },
        ],
    },
    {
        "title": "UNIT 3 Build Reliable Workflows",
        "section_label": "From one win to a system",
        "lessons": [
            {
                "title": "The research → draft → polish loop",
                "lesson_type": "read",
                "estimated_minutes": 9,
                "content": """
<p>Operators don’t “chat until it’s good.” They run a <strong>loop</strong>:</p>
<ol>
  <li><strong>Research</strong> — paste 2–3 competitor titles or a transcript chunk; ask for patterns</li>
  <li><strong>Draft</strong> — one outline, then one full script with constraints</li>
  <li><strong>Polish</strong> — shorten, punch hooks, remove fluff, match voice</li>
</ol>
<p>Save your best prompts in a note called <em>ChatGPT Ops</em>. Next week, reuse them with a new topic — that’s how you compound speed.</p>
<p><strong>MoneyTuber tip:</strong> Keep a “voice bank” — 5 sentences from scripts you liked. Paste them and say: <em>Match this cadence and vocabulary.</em></p>
""",
            },
            {
                "title": "Listen: weekly content sprint with ChatGPT",
                "lesson_type": "listen",
                "estimated_minutes": 7,
                "content": """
<p><strong>Audio script — weekly sprint</strong></p>
<p>Monday: ask ChatGPT for 20 Shorts ideas in your niche. Kill anything you wouldn’t publish. Keep seven.</p>
<p>Tuesday: turn each keeper into a one-line hook + one-line promise.</p>
<p>Wednesday–Friday: expand three into full Shorts scripts. Batch record. Batch edit.</p>
<p>Weekend: review analytics. Tell ChatGPT what retained and what died: <em>Here are my top and bottom Shorts. Suggest 5 new ideas in the style of the winners only.</em></p>
<p>That’s the machine — you stay the editor-in-chief.</p>
""",
            },
            {
                "title": "Interactive: ship your first ops prompt pack",
                "lesson_type": "interactive",
                "estimated_minutes": 10,
                "content": """
<p>Leave this lesson with a reusable pack you can run every week.</p>
""",
                "interactive_instruction": "Assemble a weekly prompt pack. Replace brackets, then save the result in your notes app.",
                "interactive_template": "WEEKLY PACK for [niche]. 1) 20 Shorts ideas. 2) Expand idea #[n] into a 40s script. 3) 8 titles + thumbnail texts. 4) Rewrite script in the voice of: [voice notes]. Constraints: [constraints].",
                "interactive_choices": "\n".join(
                    [
                        "personal finance",
                        "AI for creators",
                        "faceless storytelling",
                        "1",
                        "3",
                        "calm expert",
                        "fast and punchy",
                        "under 90 words",
                        "no emojis",
                    ]
                ),
                "interactive_check_tip": "Your pack should be copy-pasteable next Monday without rewriting the structure — only the niche and idea number change.",
            },
        ],
    },
]


def seed_sample_course(force=False):
    """Insert or replace the sample course. Returns (course, created_bool)."""
    from ..extensions import db
    from ..models import User, AcademyCourse, AcademyUnit, AcademyLesson

    existing = AcademyCourse.query.filter_by(slug=SAMPLE_SLUG).first()
    if existing and not force:
        return existing, False

    admin = (
        User.query.filter_by(role="admin", is_active_flag=True)
        .order_by(User.id.asc())
        .first()
    )
    if admin is None:
        raise RuntimeError("No admin user found — run flask create-admin first.")

    if existing and force:
        db.session.delete(existing)
        db.session.flush()

    course = AcademyCourse(
        title=COURSE["title"],
        slug=COURSE["slug"],
        summary=COURSE["summary"],
        description=COURSE["description"],
        catalog=COURSE["catalog"],
        category=COURSE["category"],
        tags=COURSE["tags"],
        estimated_lessons=COURSE["estimated_lessons"],
        estimated_hours=COURSE["estimated_hours"],
        status=COURSE["status"],
        is_featured=COURSE["is_featured"],
        sort_order=COURSE["sort_order"],
        created_by_id=admin.id,
    )
    db.session.add(course)
    db.session.flush()

    for u_idx, unit_data in enumerate(UNITS):
        unit = AcademyUnit(
            course_id=course.id,
            title=unit_data["title"],
            section_label=unit_data.get("section_label"),
            sort_order=u_idx,
        )
        db.session.add(unit)
        db.session.flush()

        for l_idx, lesson_data in enumerate(unit_data["lessons"]):
            lesson = AcademyLesson(
                unit_id=unit.id,
                title=lesson_data["title"],
                lesson_type=lesson_data["lesson_type"],
                content=(lesson_data.get("content") or "").strip() or None,
                video_embed_url=lesson_data.get("video_embed_url"),
                interactive_instruction=lesson_data.get("interactive_instruction"),
                interactive_template=lesson_data.get("interactive_template"),
                interactive_choices=lesson_data.get("interactive_choices"),
                interactive_check_tip=lesson_data.get("interactive_check_tip"),
                estimated_minutes=lesson_data.get("estimated_minutes"),
                sort_order=l_idx,
                is_published=True,
            )
            db.session.add(lesson)

    db.session.commit()
    return course, True
