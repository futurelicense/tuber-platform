from flask import Blueprint

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")

from . import routes  # noqa: E402,F401
