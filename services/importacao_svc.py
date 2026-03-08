import io
import pandas as pd
from sqlalchemy import text
from database import get_engine
from services.clientes_svc import insert_cliente

def _norm_col(c: str) -> str:
    return (c or "").strip().lower().replace(" ", "_").replace("-", "_")

def read_import_file_bytes(file_bytes: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
    elif name.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
    else:
        raise ValueError("Formato não suportado. Envie .xlsx/.xls ou .csv.")
    
    df.columns = [_norm_col(c) for c in df.columns]
    df = df.fillna("")
    return df

def validate_clientes_df(df: pd.DataFrame) -> dict:
    invalid_email = df[~df["email"].str.contains("@", na=False)]
    invalid_perfil = df[~df["perfil_decisor"].isin(["Decisor", "Influenciador"])]
    dup_df = df[df.duplicated(subset=["email", "empresa"], keep=False)]
    return {"invalid_email": invalid_email, "invalid_perfil": invalid_perfil, "dup_df": dup_df}

def import_clientes_df(df):
    engine = get_engine()
    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(
                text("""
                    INSERT INTO dbo.nps_clientes (nome, email, empresa, perfil_decisor, segmento, ativo)
                    VALUES (:nome, :email, :empresa, :perfil_decisor, :segmento, 1)
                """),
                {
                    "nome": row['nome'],
                    "email": row['email'],
                    "empresa": row['empresa'],
                    "perfil_decisor": row.get('perfil_decisor', 'Analista'),
                    "segmento": row.get('segmento', 'N/A')
                }
            )
    return {"inserted": len(df)}