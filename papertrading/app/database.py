from sqlalchemy import create_engine

from app.config import DATABASE_URL

# pool_pre_ping checks a pooled connection before using it; Neon closes idle
# connections, and without this the first request after a pause would fail.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
