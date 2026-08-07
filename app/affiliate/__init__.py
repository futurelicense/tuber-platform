from flask import Blueprint

# No url_prefix: /r/<code> must be a short public URL, not nested under
# /affiliate/, so routes spell their own full paths instead of relying on
# a blueprint-wide prefix like the other blueprints do.
bp = Blueprint("affiliate", __name__, template_folder="templates")

from . import routes  # noqa: E402,F401
