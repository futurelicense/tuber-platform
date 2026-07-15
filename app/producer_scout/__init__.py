from flask import Blueprint

bp = Blueprint("producer_scout", __name__, template_folder="templates", url_prefix="/producer-scout")

from . import routes  # noqa: E402,F401
