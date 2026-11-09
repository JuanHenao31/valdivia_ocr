"""Configuración central del servicio (pydantic-settings)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "TechApoli OCR Liquidaciones"
    debug: bool = False

    # OpenAI (visión / OCR)
    openai_api_key: str = ""
    ocr_model: str = "gpt-4o"  # Proceso 2 (remisiones/liquidaciones): estable con temperature=0
    ocr_model_planillas: str = ""  # Proceso 1 (manuscrito). Vacío -> usa ocr_model
    planilla_double_read: bool = True  # leer 2 veces y marcar lo que no coincide

    @property
    def planillas_model(self) -> str:
        return self.ocr_model_planillas or self.ocr_model

    # TypeSafe / Jev (clasificación + validación)
    typesafe_api_key: str = ""
    typesafe_model: str = "jev-latest"

    # Plantilla Excel (fija)
    template_path: str = "docs/Plantillas Liquidaciones de Fruta.xlsx"
    sheet_name: str = "Plantilla Limón"
    sheet_name_platano: str = "Plantilla Normal"

    # PDFs: páginas a mandar al OCR (Gramaluz trae 9; los datos están en la 1–2).
    max_pdf_pages: int = 3

    # Precio plátano ($/kg). Las remisiones/báscula no traen precio. General + por cliente:
    # PLATANO_PRECIOS_CLIENTE="CUATRO CIELOS=1200;OTRO=1100". Sin precio -> celda amarilla.
    platano_precio_kg: int | None = None
    platano_precios_cliente: str = ""

    # Diferencia remisión vs liquidación (kg) a partir de la cual se señala en Control.
    kg_diff_tolerance: float = 0.05

    @property
    def platano_precios(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.platano_precios_cliente.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                out[k.strip().upper()] = int(v.strip())
        return out

    def precio_platano(self, cliente: str | None) -> int | None:
        if cliente and cliente.strip().upper() in self.platano_precios:
            return self.platano_precios[cliente.strip().upper()]
        return self.platano_precio_kg

    # Umbrales para marcar celda en amarillo (revisión humana)
    ocr_conf_threshold: int = 70  # 0-100, confianza auto-reportada por el OCR
    jev_support_threshold: float = 0.7  # 0-1, confianza calibrada de Jev
    # Diccionario finca/labor (app/data/labores_por_finca.json), similitud 0-100:
    # >= ok -> se corrige al nombre oficial sin amarillo; >= review -> oficial pero amarillo;
    # menor -> se deja lo leído en amarillo.
    labor_match_ok: int = 90
    labor_match_review: int = 75

    # Clasificación de documentos entrantes (webhook): modelo barato para leer el encabezado.
    classifier_model: str = "gpt-4o-mini"

    # Canal WhatsApp (YCloud). Secretos (YCLOUD_API_KEY, YCLOUD_WEBHOOK_SECRET,
    # DOWNLOAD_URL_SECRET) se leen de os.environ, no de aquí, para no salir en un traceback.
    ycloud_enabled: bool = False
    ycloud_whatsapp_from: str = ""  # número del negocio E.164; filtra mensajes de otros números
    messaging_timeout_seconds: float = 30.0
    # Base pública: los enlaces de descarga del Excel deben ser alcanzables desde WhatsApp.
    public_base_url: str = "http://localhost:8010"
    outputs_dir: str = "output/webhook"
    download_ttl_minutes: int = 60 * 24 * 7

    # CORS (lista separada por comas)
    cors_origins: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jev_enabled(self) -> bool:
        return bool(self.typesafe_api_key)


settings = Settings()
