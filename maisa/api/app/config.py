"""Configuracion de la API por variables de entorno.

Regla dura del proyecto: **aqui no hay secretos ni rutas de maquina**. Todos los
valores son relativos o configurables; los defaults se calculan a partir de la
posicion del paquete dentro del repo, de modo que el mismo codigo funciona en
esta maquina, en otra y dentro del contenedor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

API_VERSION = "1.0.0"

# app/config.py -> app/ -> api/ -> maisa/ -> raiz del repo
APP_DIR = Path(__file__).resolve().parent
API_DIR = APP_DIR.parent
MAISA_DIR = API_DIR.parent

DEFAULT_OUTPUTS_DIR = MAISA_DIR / "outputs"
DEFAULT_FACTURAS_DIR = MAISA_DIR / "data" / "facturas"
# El BUILD del panel (`npm run build` en `maisa/ui`), no su codigo fuente.
#
# La diferencia no es cosmetica: `maisa/ui/index.html` tambien existe sin
# construir, pero es la plantilla de Vite y carga `/src/main.tsx`, que un
# navegador no sabe ejecutar. Apuntando al fuente, `index_html.is_file()` daria
# `true`, asi que /api/meta diria que hay visor y `/` serviria una pagina en
# blanco en vez de avisar de que no hay ninguno: el peor fallo posible, porque
# parece que funciona.
DEFAULT_UI_DIR = MAISA_DIR / "ui" / "dist"

# Por defecto, en Docker: el contenedor `mongo` de la red compartida. El nombre
# `mongo` es el servicio de maisa/docker-compose.yml (DNS interno de Docker),
# NO 127.0.0.1: dentro de un contenedor 127.0.0.1 es el propio contenedor.
DEFAULT_MONGO_URI = "mongodb://mongo:27017/albertitos?replicaSet=rs0&directConnection=true&authSource=albertitos"
DEFAULT_MONGO_DB = "albertitos"
# Piezas con las que se construye la URI cuando `MONGO_URI` no viene dada. El
# host por defecto es el DNS interno del contenedor de Mongo en la red
# compartida; las credenciales entran por `MONGO_APP_USER`/`MONGO_APP_PASSWORD`
# (las de `maisa/.env`, usuario de aplicacion con readWrite solo sobre
# `albertitos`), de modo que la contrasena nunca se duplica dentro de una URI.
DEFAULT_MONGO_HOST = "mongo"
DEFAULT_MONGO_PORT = 27017
DEFAULT_MONGO_REPLICA_SET = "rs0"
# Nombre DNS del contenedor del OCR en la red compartida.
DEFAULT_OCR_URL = "http://ocr-api:8866"
# El ERP solo escucha en loopback de la maquina anfitriona: desde el contenedor
# se llega por el gateway del host (ver `extra_hosts` en docker-compose.yml).
DEFAULT_ERP_URL = "http://127.0.0.1:8009"

# Origenes locales tipicos del visor. Cerrado por defecto: la lista blanca no
# incluye nada que no sea de la propia maquina.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:8010",
    "http://127.0.0.1:8010",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

DEFAULT_CRITICAL_DEPS = ("mongo", "ocr")

_TRUTHY = {"1", "true", "yes", "si", "sí", "on"}


def _env_str(name: str, default: str) -> str:
    valor = os.getenv(name)
    if valor is None:
        return default
    valor = valor.strip()
    return valor if valor else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env_str(name, "1" if default else "0").lower() in _TRUTHY


def _env_path(name: str, default: Path) -> Path:
    return Path(_env_str(name, str(default))).expanduser()


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Lista separada por comas. Vacio -> default."""
    crudo = os.getenv(name, "").strip()
    if not crudo:
        return default
    return tuple(parte.strip() for parte in crudo.split(",") if parte.strip())


def sanitize_uri(uri: str) -> str:
    """Devuelve la URI sin credenciales, apta para logs y /api/meta.

    Nunca debe salir de aqui el usuario:contrasena de Mongo.
    """
    if "@" not in uri:
        return uri
    esquema, _, resto = uri.partition("://")
    if not _:
        return "***"
    _, _, host = resto.rpartition("@")
    return f"{esquema}://***@{host}"


def mongo_uri_desde_piezas() -> str:
    """Construye la URI de Mongo con las piezas sueltas del entorno.

    Es la via que usa el contenedor: `maisa/.env` se inyecta como `env_file` y
    aporta `MONGO_APP_USER`/`MONGO_APP_PASSWORD`/`MONGO_DB`, mientras que el
    compose fija el host interno de la red Docker (`MONGO_HOST`, por defecto
    `mongo`). Asi el secreto vive en un solo sitio y el host se puede cambiar
    (contenedor o 127.0.0.1) sin tocar la contrasena.

    Usuario y contrasena se codifican con `quote_plus`: una contrasena con
    `@`, `:` o `/` romperia la URI si se pegara en crudo.

    Sin usuario definido devuelve la URI por defecto (sin credenciales), que es
    lo que quedo antes de este cambio: la API arranca igual y `/health` enseña
    `mongo` en rojo en vez de morir al importar.
    """
    usuario = os.getenv("MONGO_APP_USER", "").strip()
    if not usuario:
        return DEFAULT_MONGO_URI
    contrasena = os.getenv("MONGO_APP_PASSWORD", "").strip()
    host = _env_str("MONGO_HOST", DEFAULT_MONGO_HOST)
    puerto = _env_int("MONGO_PORT", DEFAULT_MONGO_PORT)
    db = _env_str("MONGO_DB", DEFAULT_MONGO_DB)
    credenciales = quote_plus(usuario) + (f":{quote_plus(contrasena)}" if contrasena else "")
    parametros: list[str] = []
    replica_set = _env_str("MONGO_REPLICA_SET", DEFAULT_MONGO_REPLICA_SET)
    if replica_set:
        parametros.append(f"replicaSet={quote_plus(replica_set)}")
    if _env_bool("MONGO_DIRECT_CONNECTION", True):
        parametros.append("directConnection=true")
    parametros.append(f"authSource={quote_plus(_env_str('MONGO_AUTH_SOURCE', db))}")
    return f"mongodb://{credenciales}@{host}:{puerto}/{db}?{'&'.join(parametros)}"


@dataclass(frozen=True)
class Settings:
    """Configuracion inmutable de una instancia de la API."""

    mongo_uri: str = DEFAULT_MONGO_URI
    mongo_db: str = DEFAULT_MONGO_DB
    mongo_timeout_ms: int = 1500
    ocr_url: str = DEFAULT_OCR_URL
    erp_url: str = DEFAULT_ERP_URL
    outputs_dir: Path = DEFAULT_OUTPUTS_DIR
    facturas_dir: Path = DEFAULT_FACTURAS_DIR
    ui_dir: Path = DEFAULT_UI_DIR
    api_port: int = 8010
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    cors_allow_credentials: bool = False
    api_key: str | None = None
    critical_deps: tuple[str, ...] = DEFAULT_CRITICAL_DEPS
    health_timeout_s: float = 2.0
    max_upload_mb: float = 50.0
    subidas_habilitadas: bool = True
    default_limit: int = 50
    max_limit: int = 500
    max_query_len: int = 64
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #
    # Rutas derivadas
    # ------------------------------------------------------------------ #
    @property
    def max_upload_bytes(self) -> int:
        """`MAX_UPLOAD_MB` en bytes, que es lo que compara el tope de lectura."""
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def traza_path(self) -> Path:
        """Traza encadenada del motor (una linea JSON por factura)."""
        return self.outputs_dir / "outcomes_traza.jsonl"

    @property
    def outcomes_path(self) -> Path:
        """Entrega oficial: {file_id, result}."""
        return self.outputs_dir / "outcomes.jsonl"

    @property
    def cors_abierto(self) -> bool:
        return self.cors_origins == ("*",)

    # ------------------------------------------------------------------ #
    # Construccion desde el entorno
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("API_KEY", "").strip() or None
        origins = _env_list("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
        if "*" in origins:
            # Un comodin junto con credenciales es un agujero: si alguien pide
            # "*" respetamos el comodin pero apagamos las credenciales.
            origins = ("*",)
        return cls(
            mongo_uri=_env_str("MONGO_URI", "") or mongo_uri_desde_piezas(),
            mongo_db=_env_str("MONGO_DB", DEFAULT_MONGO_DB),
            mongo_timeout_ms=_env_int("MONGO_TIMEOUT_MS", 1500),
            ocr_url=_env_str("OCR_URL", DEFAULT_OCR_URL).rstrip("/"),
            erp_url=_env_str("ERP_URL", DEFAULT_ERP_URL).rstrip("/"),
            outputs_dir=_env_path("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR),
            facturas_dir=_env_path("FACTURAS_DIR", DEFAULT_FACTURAS_DIR),
            ui_dir=_env_path("UI_DIR", DEFAULT_UI_DIR),
            api_port=_env_int("API_PORT", 8010),
            cors_origins=origins,
            cors_allow_credentials=_env_bool("CORS_ALLOW_CREDENTIALS", False)
            and origins != ("*",),
            api_key=api_key,
            critical_deps=_env_list("CRITICAL_DEPS", DEFAULT_CRITICAL_DEPS),
            health_timeout_s=_env_float("HEALTH_TIMEOUT_S", 2.0),
            max_upload_mb=_env_float("MAX_UPLOAD_MB", 50.0),
            subidas_habilitadas=_env_bool("SUBIDAS_HABILITADAS", True),
            default_limit=_env_int("DEFAULT_LIMIT", 50),
            max_limit=_env_int("MAX_LIMIT", 500),
            max_query_len=_env_int("MAX_QUERY_LEN", 64),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        )

    # ------------------------------------------------------------------ #
    # Resumen publico (para /api/meta): sin credenciales ni trazas internas
    # ------------------------------------------------------------------ #
    def publico(self) -> dict:
        # `api_version` no va aqui: `/api/meta` ya lo pone en el nivel de arriba.
        return {
            "mongo": {
                "db": self.mongo_db,
                "uri_sanitizada": sanitize_uri(self.mongo_uri),
                "timeout_ms": self.mongo_timeout_ms,
                "modo": "solo lectura del catalogo del ERP; escritura de expedientes por POST /api/facturas",
            },
            "ocr": {"url": self.ocr_url},
            "erp": {"url": self.erp_url, "nota": "solo informativo: la API no consulta el ERP"},
            "datos": {
                "outputs_dir": str(self.outputs_dir),
                "facturas_dir": str(self.facturas_dir),
                "traza_existe": self.traza_path.is_file(),
                "entrega_existe": self.outcomes_path.is_file(),
            },
            "ui": {
                "dir": str(self.ui_dir),
                "index_html": str(self.ui_dir / "index.html"),
                # Coincide con lo que hace `GET /`: sirve el visor solo si hay index.html.
                "disponible": (self.ui_dir / "index.html").is_file(),
            },
            "api": {
                "puerto": self.api_port,
                "api_key_requerida": self.api_key is not None,
                "cors_origins": list(self.cors_origins),
                "cors_abierto": self.cors_abierto,
                "cors_allow_credentials": self.cors_allow_credentials,
                "dependencias_criticas": list(self.critical_deps),
                "max_upload_mb": self.max_upload_mb,
                "limite_paginacion": {"por_defecto": self.default_limit, "maximo": self.max_limit},
                "subidas": {
                    "habilitadas": self.subidas_habilitadas,
                    "endpoint": "POST /api/facturas",
                    "campo_fichero": "file",
                    "formatos": ["application/pdf"],
                    "almacenamiento": "GridFS bucket `pdfs` + coleccion `expedientes` + traza en `eventos`",
                    "lotes": ["lote1", "lote2"],
                    "ocr": "opcional (`?ocr=true`); por defecto solo se guarda el PDF",
                },
            },
        }
