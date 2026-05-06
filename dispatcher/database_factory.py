import os
from typing import TypedDict, Optional

class ConnectionArgs(TypedDict):
    conn_str: str
    uid: Optional[str]
    pwd: Optional[str]

def build_connection_args(db_config: dict, driver: str) -> ConnectionArgs:
    """
    Constructs the connection string and kwargs for pyodbc.connect.
    Prioritizes environment variables, then falls back to config.json.
    """
    server = os.environ.get("ATANA_DB_SERVER") or db_config.get("server", "localhost")
    database = os.environ.get("ATANA_DB_NAME") or db_config.get("database", "atana")
    
    trusted_env = os.environ.get("ATANA_DB_TRUSTED")
    if trusted_env is not None:
        trusted = trusted_env.lower() in ("1", "true", "yes")
    else:
        trusted = bool(db_config.get("trusted_connection", False))

    trust_cert = "yes" if db_config.get("trust_server_certificate", True) else "no"

    if trusted:
        return {
            "conn_str": (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={database};"
                f"Trusted_Connection=yes;"
                f"TrustServerCertificate={trust_cert};"
            ),
            "uid": None,
            "pwd": None
        }
    else:
        username = os.environ.get("ATANA_DB_USER") or db_config.get("username", "")
        password = os.environ.get("ATANA_DB_PASSWORD") or db_config.get("password", "")
        
        return {
            "conn_str": (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={database};"
                f"TrustServerCertificate={trust_cert};"
            ),
            "uid": username,
            "pwd": password
        }
