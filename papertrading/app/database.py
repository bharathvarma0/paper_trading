from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql://neondb_owner:npg_VZiY4h7WtJyl@ep-divine-glitter-aoxz8wsf-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

engine= create_engine(DATABASE_URL)

