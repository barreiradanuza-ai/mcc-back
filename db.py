"""
Postgres connection helper and schema management.
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_connection():
    """Return a new psycopg2 connection using DATABASE_URL."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return psycopg2.connect(DATABASE_URL)


def create_tables():
    """Create coverage tables if they don't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ceps_claro (
                    cep CHAR(8) PRIMARY KEY
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ceps_tim (
                    cep CHAR(8) PRIMARY KEY
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cidades_promo_claro (
                    cidade TEXT PRIMARY KEY
                );
            """)
            cur.execute("DROP TABLE IF EXISTS nio_ceps")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ceps_nio (
                    cep CHAR(8) PRIMARY KEY
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS nio_cache_meta (
                    id INT PRIMARY KEY CHECK (id = 1),
                    updated_at TIMESTAMPTZ,
                    total INT
                );
            """)
        conn.commit()
    finally:
        conn.close()


def cep_has_coverage(cep: str, table: str) -> bool:
    """Return True if the given CEP exists in the specified coverage table."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} WHERE cep = %s LIMIT 1", (cep,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def city_is_promo(cidade_normalized: str) -> bool:
    """Return True if the normalized city name is in the Claro Promo list."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM cidades_promo_claro WHERE cidade = %s LIMIT 1",
                (cidade_normalized,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()
