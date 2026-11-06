"""Habilita el almacén de certificados del SO (fix para proxy/antivirus corporativo).

En redes con TLS interceptado, OpenSSL 3.x rechaza el CA inyectado. truststore hace que
Python use el almacén de Windows (que suele tener ese CA), sin desactivar la verificación.
Es idempotente: inyectar varias veces no hace daño.
"""


def enable_system_trust() -> None:
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # pragma: no cover
        pass
