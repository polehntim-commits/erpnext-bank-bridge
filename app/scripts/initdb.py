# SPDX-License-Identifier: MIT
"""One-shot: create tables. Handy for smoke testing outside Docker."""
from app import create_app, db

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        db.create_all()
        print('tables created')
