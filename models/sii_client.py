# -*- coding: utf-8 -*-
"""Punto único de integración con la librería ``facturacion_electronica`` (0.24.0).

Ruta real: models/sii_client.py

Ningún otro archivo de tf_dte_cl debe importar la librería. Este módulo:

* neutraliza los efectos globales que la librería provoca al importarse
  (desactiva la verificación TLS del proceso y cambia el codificador multipart
  de urllib3);
* entrega a la librería copias limpias de los datos (sin valores vacíos y con
  fechas como texto), porque ``set_from_keys`` muta el dict recibido y los
  getters fallan con ``False``/``None``;
* normaliza sus respuestas, que nunca lanzan excepciones sino que devuelven el
  error como dato, y las interpreta a partir de los códigos crudos del SII.

Debe ser el primer import de ``models/__init__.py``.
"""
from __future__ import annotations

import codecs
import logging
import re
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date

from lxml import etree

from odoo import api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

LIB_VERSION = '0.24.0'

# Alcance del núcleo B2B: cualquier otro TipoDTE (boletas, exportación, 46, 43)
# se rechaza antes de llegar a la librería.
SUPPORTED_DTE_TYPES = frozenset({33, 34, 52, 56, 61})
SII_ENVIRONMENTS = frozenset({'certificacion', 'produccion'})

# ---------------------------------------------------------------------------
# Import aislado de la librería
# ---------------------------------------------------------------------------
_UTF8_WRITER = codecs.lookup('utf-8')[3]        # valor por defecto de urllib3
_ISO_WRITER = codecs.lookup('ISO-8859-1')[3]    # lo que la librería necesita al enviar

_ssl_default_context = ssl._create_default_https_context
if _ssl_default_context is ssl._create_unverified_context:
    # Otro módulo importó la librería (o desactivó TLS) antes que tf_dte_cl.
    _logger.warning(
        'La verificación TLS del proceso ya estaba desactivada antes de cargar tf_dte_cl; '
        'se restablece la verificación por defecto.'
    )
    _ssl_default_context = ssl.create_default_context

try:
    from urllib3 import filepost as _filepost
except ImportError:  # pragma: no cover - urllib3 es dependencia de requests/Odoo
    _filepost = None

try:
    from facturacion_electronica import facturacion_electronica as fe
except ImportError:
    fe = None
    _logger.warning(
        'No se pudo importar "facturacion_electronica". Instálela con: '
        'pip install facturacion-electronica==%s', LIB_VERSION,
    )
finally:
    ssl._create_default_https_context = _ssl_default_context
    if _filepost is not None:
        _filepost.writer = _UTF8_WRITER

_send_lock = threading.Lock()


@contextmanager
def _iso_multipart():
    """Aplica el codificador ISO-8859-1 solo durante el upload al SII.

    El lock serializa los envíos de tf_dte_cl; otro hilo que suba un multipart
    en esa misma ventana también usará ISO-8859-1 (riesgo acotado y documentado).
    """
    if _filepost is None:
        yield
        return
    with _send_lock:
        _filepost.writer = _ISO_WRITER
        try:
            yield
        finally:
            _filepost.writer = _UTF8_WRITER


class _RedactXmlFilter(logging.Filter):
    """Evita que la librería escriba DTE completos en el log del servidor."""

    max_length = 300

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - un log mal formado no debe romper nada
            return True
        if len(message) > self.max_length and '<' in message and '>' in message:
            record.msg = 'XML omitido del log (%s caracteres).'
            record.args = (len(message),)
        return True


# Los filtros de un logger padre no se aplican a los registros de sus hijos,
# por eso se agrega a cada logger de la librería que puede volcar XML.
_redact_filter = _RedactXmlFilter()
for _logger_name in (
    'facturacion_electronica.facturacion_electronica',
    'facturacion_electronica.clase_util',
    'facturacion_electronica.dte',
    'facturacion_electronica.envio',
    'facturacion_electronica.conexion',
    'facturacion_electronica.firma',
    'facturacion_electronica.signature_cert',
):
    logging.getLogger(_logger_name).addFilter(_redact_filter)

# ---------------------------------------------------------------------------
# Interpretación de códigos del SII
# ---------------------------------------------------------------------------
# QueryEstUp (manual SII OI2004_CEUPDTE_MDE_1.10, punto 3.3): RSC, RCT y RFR son
# rechazos del manual; el resto proviene de clase_util.estado_envio (sin libros).
UPLOAD_REJECTED_CODES = frozenset({'RCT', 'RCH', 'RFR', 'RSC', 'RDC', 'RCR', 'RCO', 'RCS', 'FNA', '106'})
UPLOAD_ACCEPTED_CODE = 'EPR'
UPLOAD_COUNTERS = ('INFORMADOS', 'ACEPTADOS', 'RECHAZADOS', 'REPAROS')
# STATUS del upload que la librería asocia a esquema inválido (7) o firma (8).
UPLOAD_FATAL_STATUS = frozenset({'7', '8'})

# QueryEstDte (manual SII OI2004_CEDTE_MDE_1.10, tabla 3-4):
#   DOK/DNK: recibido (datos coinciden / no coinciden); FAU: no recibido;
#   FNA: no autorizado; FAN: anulado; EMP: empresa no autorizada;
#   TMD/TMC/MMD/MMC/AND/ANC: recibido y modificado o anulado por una nota.
# FNA y EMP no permiten concluir si el documento llegó: quedan como None.
DTE_NOT_RECEIVED_CODES = frozenset({'FAU'})
DTE_RECEIVED_CODES = frozenset({'DOK', 'DNK', 'FAN', 'TMD', 'TMC', 'MMD', 'MMC', 'AND', 'ANC'})

SEND_OK = 'ok'            # carga aceptada, hay Track ID
SEND_RETRY = 'retry'      # la carga no fue aceptada; se puede reintentar el mismo sobre
SEND_UNKNOWN = 'unknown'  # no se sabe si llegó; consultar antes de reenviar
SEND_FAILED = 'failed'    # esquema/firma inválidos; requiere corrección y nueva firma

UPLOAD_STATE_ACCEPTED = 'accepted'
UPLOAD_STATE_OBJECTIONS = 'accepted_objections'
UPLOAD_STATE_REJECTED = 'rejected'

_XML_ID_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]{0,79}$')


@dataclass
class SignResult:
    """Resultado de timbrar y firmar un DTE."""
    ok: bool
    tipo_dte: int | None = None
    folio: int | None = None
    xml: str | None = None
    ted: str | None = None
    barcode_png_b64: str | None = None
    amounts: dict = field(default_factory=dict)
    message: str = ''


@dataclass
class EnvelopeResult:
    """Resultado de armar y firmar un sobre EnvioDTE sin transmitirlo."""
    ok: bool
    xml: str | None = None
    filename: str | None = None
    message: str = ''


@dataclass
class SendResult:
    """Resultado de transmitir un sobre ya firmado."""
    outcome: str
    track_id: str | None = None
    upload_status: str | None = None
    message: str = ''


@dataclass
class QueryResult:
    """Resultado de una consulta al SII.

    ``ok=False`` significa que la consulta falló: el estado del documento no debe
    modificarse. ``state`` solo se informa en la consulta de envío y es ``None``
    mientras el SII no tenga un resultado definitivo.
    """
    ok: bool
    code: str | None = None
    state: str | None = None
    received: bool | None = None
    counters: dict = field(default_factory=dict)
    detail: str = ''
    raw: str | None = None


# ---------------------------------------------------------------------------
# Utilidades puras (sin Odoo), cubiertas por tests unitarios
# ---------------------------------------------------------------------------
def clean_payload(value):
    """Copia profunda sin valores vacíos y con fechas como texto 'YYYY-MM-DD'.

    Se conservan los ceros: ``NroResol`` es 0 en certificación.
    """
    if isinstance(value, dict):
        cleaned = type(value)()
        for key, item in value.items():
            item = clean_payload(item)
            if item is None or item is False or item == '' or item == [] or item == {}:
                continue
            cleaned[key] = item
        return cleaned
    if isinstance(value, (list, tuple)):
        return [clean_payload(item) for item in value]
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    return value


def short_message(error, limit: int = 500) -> str:
    """Primera línea útil de un error, sin traceback."""
    text = str(error or '').strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), '')
    return first[:limit]


def errors_to_text(errors) -> str:
    """Normaliza el campo ``errores`` de la librería (str, lista de str o de dict)."""
    if not errors:
        return ''
    if isinstance(errors, (str, bytes)):
        errors = [errors]
    parts = []
    for item in errors:
        if isinstance(item, dict):
            prefix = ''
            if item.get('TipoDTE') and item.get('Folio'):
                prefix = 'T%sF%s: ' % (item['TipoDTE'], item['Folio'])
            parts.append(prefix + short_message(item.get('error')))
        else:
            parts.append(short_message(item))
    return '; '.join(filter(None, parts))


def parse_sii_xml(raw) -> dict:
    """Aplana una respuesta XML del SII en ``{tag: texto}`` (prevalece el primero)."""
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    if not isinstance(raw, str):
        return {}
    raw = re.sub(r'^\s*<\?xml[^>]*\?>', '', raw).replace('SII:', '')
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(raw.encode('utf-8'), parser)
    except (etree.XMLSyntaxError, ValueError):
        return {}
    values = {}
    for element in root.iter():
        if isinstance(element.tag, str) and element.text and element.text.strip():
            values.setdefault(etree.QName(element).localname, element.text.strip())
    return values


def _upload_status_code(raw) -> str | None:
    values = parse_sii_xml(raw)
    return values.get('STATUS')


def classify_send_response(resp) -> SendResult:
    """Clasifica la respuesta de ``fe.enviar_xml``."""
    if isinstance(resp, str):
        if resp:
            # 'DTE Ya se encuentra en envío': otro hilo del mismo proceso está enviando.
            return SendResult(SEND_RETRY, message=short_message(resp))
        return SendResult(
            SEND_UNKNOWN,
            message='La librería no devolvió respuesta; se consultará al SII antes de reenviar.',
        )
    if not isinstance(resp, dict) or not resp:
        return SendResult(SEND_UNKNOWN, message='Respuesta vacía de la librería al enviar.')
    if resp.get('sii_send_ident'):
        return SendResult(SEND_OK, track_id=str(resp['sii_send_ident']).strip(), upload_status='0')
    if 'sii_xml_response' not in resp:
        # Excepción de transporte dentro de la librería: el sobre pudo haber llegado.
        return SendResult(
            SEND_UNKNOWN,
            message='Error de comunicación al enviar al SII: %s' % short_message(resp.get('xml_resp')),
        )
    code = _upload_status_code(resp.get('sii_xml_response'))
    if code in UPLOAD_FATAL_STATUS:
        return SendResult(
            SEND_FAILED, upload_status=code,
            message='El SII rechazó la carga (STATUS %s: esquema o firma inválidos).' % code,
        )
    return SendResult(
        SEND_RETRY, upload_status=code,
        message='El SII no aceptó la carga (STATUS %s).' % (code or 'HTTP'),
    )


def interpret_upload_values(values: dict) -> str | None:
    """Estado definitivo de un sobre con un solo DTE, o ``None`` si sigue en proceso."""
    code = values.get('ESTADO_ENVIO') or values.get('ESTADO')
    if code == UPLOAD_ACCEPTED_CODE:
        if values.get('RECHAZADOS', '0') != '0':
            return UPLOAD_STATE_REJECTED
        if values.get('REPAROS', '0') != '0':
            return UPLOAD_STATE_OBJECTIONS
        if values.get('ACEPTADOS', '0') != '0':
            return UPLOAD_STATE_ACCEPTED
        return None
    if code in UPLOAD_REJECTED_CODES:
        return UPLOAD_STATE_REJECTED
    return None


def document_was_received(code: str | None) -> bool | None:
    """``True``/``False`` si el código de QueryEstDte es concluyente; ``None`` si no."""
    if code in DTE_NOT_RECEIVED_CODES:
        return False
    if code in DTE_RECEIVED_CODES:
        return True
    return None


def _glosa(values: dict) -> str:
    parts = [values.get(tag) for tag in ('GLOSA_ESTADO', 'GLOSA', 'DESC_ESTADO', 'GLOSA_ERR')]
    return ' - '.join(dict.fromkeys(filter(None, parts)))


def _raw_text(raw) -> str | None:
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return raw if isinstance(raw, str) else None


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------
class TfDteClSiiClient(models.AbstractModel):
    _name = 'tf_dte_cl.sii.client'
    _description = 'Cliente SII (integración con facturacion_electronica)'

    # -- Validaciones previas ------------------------------------------------
    @api.model
    def _tf_dte_cl_check_library(self):
        if fe is None:
            raise UserError(self.env._(
                'La librería "facturacion_electronica" no está instalada en el servidor '
                '(versión esperada: %s).', LIB_VERSION,
            ))

    @api.model
    def _tf_dte_cl_check_payload(self, payload: dict, documents: bool = False) -> None:
        emisor = payload.get('Emisor') or {}
        if emisor.get('Modo') not in SII_ENVIRONMENTS:
            raise UserError(self.env._('Ambiente SII inválido: use "certificacion" o "produccion".'))
        if not emisor.get('RUTEmisor'):
            raise UserError(self.env._('Falta el RUT del emisor.'))
        firma = payload.get('firma_electronica') or {}
        missing = [key for key in ('priv_key', 'cert', 'rut_firmante') if not firma.get(key)]
        if missing:
            raise UserError(self.env._('El certificado digital está incompleto (%s).', ', '.join(missing)))
        if not documents:
            return
        groups = payload.get('Documento') or []
        if not groups or not all(group.get('documentos') for group in groups):
            raise UserError(self.env._('No hay documentos para procesar.'))
        for group in groups:
            if group.get('TipoDTE') not in SUPPORTED_DTE_TYPES:
                raise UserError(self.env._(
                    'El tipo de documento %s no está dentro del alcance de este módulo.',
                    group.get('TipoDTE'),
                ))

    @api.model
    def _tf_dte_cl_check_envelope_ids(self, payload: dict) -> None:
        if not _XML_ID_RE.match(str(payload.get('ID') or '')):
            raise UserError(self.env._('Identificador de sobre inválido: %s.', payload.get('ID')))
        if not payload.get('filename'):
            raise UserError(self.env._('Falta el nombre de archivo del sobre.'))

    # -- Etapa 1: timbrar y firmar --------------------------------------------
    @api.model
    def tf_dte_cl_sign(self, payload: dict) -> list[SignResult]:
        """Genera XML + TED y firma cada DTE sin enviarlo (``fe.timbrar``).

        Nunca debe llamarse con ``sii_xml_request`` en los documentos: la librería
        volvería a timbrar con otro TSTED y a firmar.
        """
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload, documents=True)
        for group in payload['Documento']:
            if any(doc.get('sii_xml_request') for doc in group['documentos']):
                raise UserError(self.env._('No se puede volver a timbrar un documento ya firmado.'))
        try:
            responses = fe.timbrar(payload)
        except Exception as error:  # noqa: BLE001 - la librería puede fallar antes de su propio try
            _logger.exception('facturacion_electronica.timbrar falló')
            return [SignResult(ok=False, message=short_message(error))]
        results = []
        for resp in responses or []:
            result = SignResult(
                ok=False,
                tipo_dte=resp.get('TipoDTE'),
                folio=resp.get('Folio'),
                amounts={
                    key: resp.get(key)
                    for key in ('MntExe', 'MntNeto', 'MntIVA', 'ImptoReten', 'MntTotal')
                },
            )
            if resp.get('error'):
                result.message = short_message(resp['error'])
            elif not resp.get('sii_xml_dte') or not resp.get('sii_barcode'):
                result.message = self.env._('La librería no generó el XML firmado o el timbre (TED).')
            else:
                result.ok = True
                result.xml = resp['sii_xml_dte']
                result.ted = resp['sii_barcode']
                result.barcode_png_b64 = resp.get('sii_barcode_img') or None
            results.append(result)
        if not results:
            return [SignResult(ok=False, message=self.env._('La librería no generó ningún documento.'))]
        return results

    # -- Etapa 2: armar el sobre ----------------------------------------------
    @api.model
    def tf_dte_cl_build_envelope(self, payload: dict) -> EnvelopeResult:
        """Arma y firma el EnvioDTE con DTE ya firmados, sin transmitirlo (``fe.xml_envio``)."""
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload, documents=True)
        self._tf_dte_cl_check_envelope_ids(payload)
        for group in payload['Documento']:
            if not all(doc.get('sii_xml_request') for doc in group['documentos']):
                raise UserError(self.env._('Todos los documentos del sobre deben estar firmados previamente.'))
        try:
            resp = fe.xml_envio(payload)
        except Exception as error:  # noqa: BLE001
            _logger.exception('facturacion_electronica.xml_envio falló (sobre %s)', payload['ID'])
            return EnvelopeResult(ok=False, message=short_message(error))
        if not isinstance(resp, dict):
            return EnvelopeResult(ok=False, message=self.env._('Respuesta inesperada al armar el sobre.'))
        if resp.get('errores'):
            return EnvelopeResult(ok=False, message=errors_to_text(resp['errores']))
        if not resp.get('sii_xml_request'):
            return EnvelopeResult(ok=False, message=self.env._('La librería no generó el sobre EnvioDTE.'))
        return EnvelopeResult(
            ok=True,
            xml=resp['sii_xml_request'],
            filename=resp.get('sii_send_filename') or payload['filename'],
        )

    # -- Etapa 3: transmitir ---------------------------------------------------
    @api.model
    def tf_dte_cl_send_envelope(self, payload: dict) -> SendResult:
        """Transmite un sobre ya firmado y guardado (``fe.enviar_xml``)."""
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload)
        self._tf_dte_cl_check_envelope_ids(payload)
        if not payload.get('sii_xml_request'):
            raise UserError(self.env._('No hay un sobre firmado para enviar.'))
        try:
            with _iso_multipart():
                resp = fe.enviar_xml(payload)
        except Exception as error:  # noqa: BLE001
            _logger.exception('facturacion_electronica.enviar_xml falló (sobre %s)', payload['ID'])
            return SendResult(SEND_UNKNOWN, message=short_message(error))
        result = classify_send_response(resp)
        _logger.info('Envío SII del sobre %s: %s %s', payload['ID'], result.outcome, result.track_id or '')
        return result

    # -- Etapa 4: consultar ----------------------------------------------------
    @api.model
    def tf_dte_cl_query_upload(self, payload: dict) -> QueryResult:
        """Estado de un sobre por Track ID (``fe.consulta_estado_envio``)."""
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload)
        if not payload.get('codigo_envio'):
            raise UserError(self.env._('Falta el Track ID para consultar el envío.'))
        try:
            resp = fe.consulta_estado_envio(payload)
        except Exception as error:  # noqa: BLE001 - procesar_respuesta_envio no captura sus errores
            _logger.exception('Consulta de envío %s falló', payload['codigo_envio'])
            return QueryResult(ok=False, detail=short_message(error))
        if not isinstance(resp, dict):
            return QueryResult(ok=False, detail=self.env._('Respuesta inesperada del SII.'))
        if resp.get('errores'):
            return QueryResult(ok=False, detail=errors_to_text(resp['errores']))
        if resp.get('warning') and not resp.get('xml_resp'):
            return QueryResult(ok=False, detail=short_message(resp['warning'].get('message')))
        raw = _raw_text(resp.get('xml_resp'))
        values = parse_sii_xml(raw)
        code = values.get('ESTADO_ENVIO') or values.get('ESTADO')
        detail = _glosa(values)
        if not code or code.startswith('-'):
            return QueryResult(ok=False, code=code, detail=detail or self.env._('El SII no informó un estado.'), raw=raw)
        return QueryResult(
            ok=True,
            code=code,
            state=interpret_upload_values(values),
            counters={key: values[key] for key in UPLOAD_COUNTERS if key in values},
            detail=detail,
            raw=raw,
        )

    @api.model
    def tf_dte_cl_query_document(self, payload: dict) -> QueryResult:
        """Estado de un DTE puntual (``fe.consulta_estado_dte``).

        Cada documento requiere: Folio, FchEmis, Receptor.RUTRecep y MntTotal.
        """
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload, documents=True)
        if sum(len(group['documentos']) for group in payload['Documento']) != 1:
            raise UserError(self.env._('La consulta de estado DTE se hace de a un documento.'))
        try:
            resp = fe.consulta_estado_dte(payload)
        except Exception as error:  # noqa: BLE001
            _logger.exception('Consulta de estado DTE falló')
            return QueryResult(ok=False, detail=short_message(error))
        item = next(iter(resp.values()), None) if isinstance(resp, dict) and resp else None
        if not isinstance(item, dict):
            return QueryResult(ok=False, detail=self.env._('Respuesta inesperada del SII.'))
        if item.get('errores'):
            return QueryResult(ok=False, detail=errors_to_text(item['errores']))
        raw = _raw_text(item.get('xml_resp'))
        values = parse_sii_xml(raw)
        code = values.get('ESTADO')
        detail = _glosa(values) or item.get('glosa') or ''
        if not code or code.startswith('-'):
            return QueryResult(ok=False, code=code, detail=detail or self.env._('El SII no informó un estado.'), raw=raw)
        return QueryResult(ok=True, code=code, received=document_was_received(code), detail=detail, raw=raw)
