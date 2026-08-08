from flask import Blueprint, session, request

bp = Blueprint("academy", __name__, template_folder="templates", url_prefix="/academy")


@bp.context_processor
def _inject_ref_code():
    """Keep affiliate ?ref= sticky across Academy browse → signup."""
    from_query = (request.args.get("ref") or "").strip().upper()
    if from_query:
        session["ref_code"] = from_query
        return {"ref_code": from_query}
    return {"ref_code": (session.get("ref_code") or "").strip() or None}


from . import routes  # noqa: E402,F401
