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
    planilla_workers: int = 8  # docs de planilla procesados en paralelo (1 = secuencial)

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
    # Proceso 2: documentos procesados en paralelo (OCR + Jev, I/O). 1 = secuencial.
    pipeline_workers: int = 8

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

    # OCR robusto (Structured Outputs + reintentos). Topes de salida: cortan los bucles
    # degenerados del modelo en raw_text. Planillas: ~150-200 tokens por fila + raw_text.
    # Plazos TOTALES (reloj de pared) por llamada: el OCR va en streaming y corta la llamada
    # al superarlos (ocr_service), no son timeouts por fase de httpx.
    openai_timeout_s: float = 30.0  # plazo total por llamada por defecto (clasificador...)
    openai_timeout_planillas_s: float = 90.0  # planillas generan hasta ~4000 tokens
    openai_timeout_documentos_s: float = 45.0  # Proceso 2 (remisiones/liquidaciones)
    # Tope de salida POR PÁGINA en Proceso 2 (PDF de N páginas -> N x tope, hasta el techo).
    ocr_max_tokens_documentos: int = 3000  # incluye raw_text para Jev (+ razonamiento en gpt-5)
    # Planillas: en gpt-5 el tope incluye los tokens de razonamiento (~1-3k con effort=low).
    ocr_max_tokens_planillas: int = 6000
    # Techo: al truncarse (finish_reason=length) el reintento duplica el tope hasta aquí.
    ocr_max_tokens_tope: int = 12000
    # gpt-5 / o-series: reasoning_effort enviado en cada llamada ("" = no se envía).
    ocr_reasoning_effort: str = "low"
    ocr_max_attempts: int = 3
    ocr_retry_temperature: float = 0.2
    ocr_backoff_base_s: float = 1.0

    # Canal WhatsApp (YCloud). Secretos (YCLOUD_API_KEY, YCLOUD_WEBHOOK_SECRET,
    # DOWNLOAD_URL_SECRET) se leen de os.environ, no de aquí, para no salir en un traceback.
    ycloud_enabled: bool = False
    ycloud_whatsapp_from: str = ""  # número del negocio E.164; filtra mensajes de otros números
    messaging_timeout_seconds: float = 30.0
    # Base pública: los enlaces de descarga del Excel deben ser alcanzables desde WhatsApp.
    public_base_url: str = "http://localhost:8010"
    outputs_dir: str = "output/webhook"
    download_ttl_minutes: int = 60 * 24 * 7
    # Media entrante persistida hasta que su lote se responde (sobrevive a reinicios).
    # Vacío -> <outputs_dir>/pending (mismo volumen Docker, sin montajes extra).
    pending_dir: str = ""
    pending_recovery_delay_s: float = 5.0  # al arrancar, espera antes de vaciar lotes recuperados
    processed_ids_max: int = 20000  # msg_ids recordados en disco para dedup tras reinicio
    pending_max_intentos: int = 3  # recuperaciones de un ítem antes de apartarlo a pending/_fallidos
    shutdown_timeout_s: float = 8.0  # al apagar, espera máxima a vaciados en curso (< 10 s de Docker)

    @property
    def pending_path(self) -> str:
        import os

        return self.pending_dir or os.path.join(self.outputs_dir, "pending")

    # CORS (lista separada por comas)
    cors_origins: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jev_enabled(self) -> bool:
        return bool(self.typesafe_api_key)


settings = Settings()
