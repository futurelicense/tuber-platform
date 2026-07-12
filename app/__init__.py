import os

import click
from flask import Flask

from .config import Config
from .extensions import db, login_manager, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from .auth import bp as auth_bp
    from .admin import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    register_cli(app)

    return app


def register_cli(app):
    @app.cli.command("create-admin")
    @click.argument("email")
    @click.argument("password")
    def create_admin(email, password):
        """Bootstrap the first admin account: flask create-admin you@example.com yourpassword"""
        from .models import User

        email = email.strip().lower()
        if User.query.filter_by(email=email).first():
            click.echo(f"User {email} already exists.")
            return
        user = User(email=email, role="admin", display_name="Admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created admin {email}.")
