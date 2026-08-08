from flask import Blueprint

bp = Blueprint("academy", __name__, template_folder="templates", url_prefix="/academy")

from . import routes  # noqa: E402,F401
