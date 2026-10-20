from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql+psycopg2://bharathvarna@localhost:5432/papertrading"

engine= create_engine(DATABASE_URL)

