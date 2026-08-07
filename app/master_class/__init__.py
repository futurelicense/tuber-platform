from flask import Blueprint

bp = Blueprint("master_class", __name__, template_folder="templates", url_prefix="/master-class")

from . import routes  # noqa: E402,F401
