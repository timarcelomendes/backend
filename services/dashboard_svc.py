import pandas as pd
from sqlalchemy import text
from datetime import date, timedelta
from database import get_engine

def read_df(sql: str, params: dict = None) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text(sql), params or {})
        rows = res.fetchall()
        cols = list(res.keys())
    return pd.DataFrame(rows, columns=cols)

def get_kpis_data(periodo_dias: int, empresa_sel: str) -> pd.DataFrame:
    hoje = date.today()
    ini_atual = hoje - timedelta(days=periodo_dias)
    ini_anterior = ini_atual - timedelta(days=periodo_dias)
    
    empresa_where = " AND empresa = :empresa " if empresa_sel != "Todas" else ""
    params_base = {
        "ini_atual": str(ini_atual), "fim_atual": str(hoje),
        "ini_ant": str(ini_anterior), "fim_ant": str(ini_atual),
    }
    if empresa_sel != "Todas":
        params_base["empresa"] = empresa_sel

    sql_kpis = f"""
    WITH base AS (
      SELECT
        CAST(data_resposta AS DATE) AS dia,
        CASE
          WHEN CAST(data_resposta AS DATE) >= CAST(:ini_atual AS DATE) AND CAST(data_resposta AS DATE) <= CAST(:fim_atual AS DATE) THEN 'atual'
          WHEN CAST(data_resposta AS DATE) >= CAST(:ini_ant AS DATE) AND CAST(data_resposta AS DATE) < CAST(:fim_ant AS DATE) THEN 'anterior'
          ELSE NULL
        END AS periodo,
        nota
      FROM dbo.nps_respostas
      WHERE deleted_at IS NULL AND data_resposta IS NOT NULL {empresa_where}
    ),
    agg AS (
      SELECT periodo, COUNT(1) AS total,
        SUM(CASE WHEN nota >= 9 THEN 1 ELSE 0 END) AS promotores,
        SUM(CASE WHEN nota BETWEEN 7 AND 8 THEN 1 ELSE 0 END) AS neutros,
        SUM(CASE WHEN nota <= 6 THEN 1 ELSE 0 END) AS detratores
      FROM base WHERE periodo IS NOT NULL GROUP BY periodo
    )
    SELECT * FROM agg;
    """
    return read_df(sql_kpis, params_base)

def get_empresas_disponiveis() -> list:
    df_emp = read_df("SELECT DISTINCT empresa FROM dbo.nps_respostas WHERE deleted_at IS NULL AND empresa IS NOT NULL AND LTRIM(RTRIM(empresa)) <> '' ORDER BY empresa ASC;")
    return ["Todas"] + df_emp["empresa"].dropna().astype(str).tolist() if not df_emp.empty else ["Todas"]