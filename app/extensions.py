from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
# Protects every platform POST route (all are plain HTML forms carrying the
# csrf_token hidden input). The vendored apps under /clip and /produce are
# separate Flask instances reached through DispatcherMiddleware — this
# extension never sees their requests. Those routes get their own,
# token-free CSRF defense instead: app/mounting/origin_check.py.
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
