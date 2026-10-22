import os

from dotenv import load_dotenv

# Read a local .env file (gitignored) so secrets never live in committed code.
# Real environment variables win over .env values.
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://localhost:5432/papertrading")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-me")
JWT_ALGORITHM = "HS256"
TOKEN_MINUTES = int(os.getenv("TOKEN_MINUTES", "60"))
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")
