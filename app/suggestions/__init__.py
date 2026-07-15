from flask import Blueprint

bp = Blueprint("suggestions", __name__, template_folder="templates", url_prefix="/suggestions")

from . import routes  # noqa: E402,F401
