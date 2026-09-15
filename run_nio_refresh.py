"""Quinzenal refresh: extract both datasets, validate, then replace tables atomically."""
import csv, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import psycopg2, psycopg2.extras

ROOT = Path(__file__).parent
CEP_CSV = ROOT / "ceps_nio_novos.csv"
FACADE_CSV = ROOT / "super_lista_nio_staging.csv"
AUDIT = ROOT / "super_lista_nio_auditoria.json"
DATABASE_URL = os.environ["DATABASE_URL"]

def run(command):
    print("[refresh]", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True, env={**os.environ, "NIO_EXPORT_ONLY": "true"})

def read_ceps():
    with CEP_CSV.open(newline="", encoding="utf-8") as f:
        values = [row["CEP"].strip() for row in csv.DictReader(f)]
    values = sorted({v for v in values if len(v) == 8 and v.isdigit()})
    if len(values) < 100000: raise RuntimeError(f"CEP extraction too small: {len(values)}")
    return values

def read_facades():
    with FACADE_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    required = {"CEP", "UF", "MUNICIPIO", "NO_FACHADA", "VIABILIDADE_ATUAL"}
    if not rows or not required.issubset(rows[0]): raise RuntimeError("Facade CSV missing required columns")
    return rows

def replace_database(ceps, rows):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE ceps_nio_stage (cep CHAR(8) PRIMARY KEY) ON COMMIT DROP")
            psycopg2.extras.execute_values(cur, "INSERT INTO ceps_nio_stage (cep) VALUES %s", [(x,) for x in ceps], page_size=10000)
            cur.execute("CREATE TEMP TABLE super_lista_nio_stage (LIKE super_lista_nio INCLUDING DEFAULTS) ON COMMIT DROP")
            cols = ["cep","uf","municipio","bairro","logradouro","no_fachada","complemento1","complemento2","complemento3","codigo_logradouro","codigo_cdo","classificacao","celula","estacao","viabilidade_atual","regiao","relatorio_origem"]
            values = [tuple(r.get(k.upper(), "") or None for k in cols) for r in rows]
            psycopg2.extras.execute_values(cur, f"INSERT INTO super_lista_nio_stage ({','.join(cols)}) VALUES %s", values, page_size=5000)
            cur.execute("TRUNCATE ceps_nio")
            cur.execute("INSERT INTO ceps_nio SELECT cep FROM ceps_nio_stage")
            cur.execute("TRUNCATE super_lista_nio")
            cur.execute("INSERT INTO super_lista_nio (" + ",".join(cols) + ") SELECT " + ",".join(cols) + " FROM super_lista_nio_stage")
            now = datetime.now(timezone.utc)
            cur.execute("INSERT INTO nio_cache_meta (id,updated_at,total) VALUES (1,%s,%s) ON CONFLICT(id) DO UPDATE SET updated_at=%s,total=%s", (now,len(ceps),now,len(ceps)))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()

def main():
    run([sys.executable, "sync_nio_ceps.py"])
    run([sys.executable, "extract_nio_facades.py"])
    ceps, rows = read_ceps(), read_facades()
    print(f"[refresh] validated {len(ceps)} CEPs and {len(rows)} facade rows", flush=True)
    replace_database(ceps, rows)
    print("[refresh] atomic database replacement completed", flush=True)

if __name__ == "__main__": main()
