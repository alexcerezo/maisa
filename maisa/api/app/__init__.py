"""API/BFF de Albertitos: la unica superficie expuesta del sistema.

Este paquete NO contiene logica de decision. Es una capa de lectura y de
composicion:

  * lee la traza y la entrega del motor desde disco (solo lectura),
  * lee el catalogo del ERP desde MongoDB (solo lectura, `find`/`aggregate`),
  * hace de proxy del servicio de OCR,
  * sirve el visor estatico en el mismo origen.

Ver README.md para el contrato de cada endpoint y las variables de entorno.
"""

from .config import API_VERSION

__all__ = ["API_VERSION"]
