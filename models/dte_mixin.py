# -*- coding: utf-8 -*-
"""Ciclo de vida común de un DTE, compartido por facturas/notas y guías.

Ruta real: models/dte_mixin.py

Etapas:
1. ``_tf_dte_cl_prepare()``  (síncrono, sin red; al publicar o validar)
   validación completa → folio → timbre y firma → sobre firmado.
   Cualquier error revierte la transacción y el folio vuelve al CAF.
2. ``_tf_dte_cl_send()``  (cron o framework EDI)
   intento registrado en transacción propia → envío del sobre guardado.
3. ``_tf_dte_cl_query()``  (cron)
   consulta del envío por Track ID o, sin Track ID, del documento.

Los modelos concretos implementan los métodos ``_tf_dte_cl_get_*`` y
``_tf_dte_cl_document_values``.
"""
from __future__ import annotations

import base64
import logging
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .caf import DTE_TYPES
from .dte_lines import SII_TAXES, compute_totals, round_half_up
from .res_partner import normalize_rut
from .sii_client import (
    SEND_FAILED, SEND_OK, SEND_RETRY, SEND_UNKNOWN,
    UPLOAD_STATE_ACCEPTED, UPLOAD_STATE_OBJECTIONS, UPLOAD_STATE_REJECTED,
)

_logger = logging.getLogger(__name__)

DTE_STATES = [
    ('to_sign', 'Por firmar'),
    ('signed', 'Firmado, pendiente de envío'),
    ('send_unknown', 'Envío sin confirmar'),
    ('sent', 'Enviado, en revisión del SII'),
    ('accepted', 'Aceptado'),
    ('accepted_objections', 'Aceptado con reparos'),
    ('rejected', 'Rechazado'),
    ('error', 'Error'),
    ('voided', 'Folio anulado'),
]
FINAL_STATES = ('accepted', 'accepted_objections', 'rejected', 'voided')
VALID_STATES = ('accepted', 'accepted_objections')
PENDING_QUERY_STATES = ('sent', 'send_unknown')
DATE_MIN = date(2000, 1, 1)     # SiiTypes FechaType
DATE_MAX = date(2050, 12, 31)
QUERY_INTERVAL_MINUTES = 5
DEFAULT_RESEND_MINUTES = 60

# QueryEstDte: recibido con datos coincidentes, o recibido y luego modificado/anulado por una nota.
DOCUMENT_ACCEPTED_CODES = frozenset({'DOK', 'TMD', 'TMC', 'MMD', 'MMC', 'AND', 'ANC'})

RESOLVE_RECEIVED = 'received'
RESOLVE_RESEND = 'resend'
RESOLVE_WAIT = 'wait'


# ---------------------------------------------------------------------------
# Decisiones puras (sin Odoo), cubiertas por tests unitarios
# ---------------------------------------------------------------------------
def state_after_send(outcome: str) -> str:
    return {
        SEND_OK: 'sent',
        SEND_RETRY: 'signed',
        SEND_UNKNOWN: 'send_unknown',
        SEND_FAILED: 'error',
    }[outcome]


def state_from_upload(upload_state: str | None) -> str | None:
    return {
        UPLOAD_STATE_ACCEPTED: 'accepted',
        UPLOAD_STATE_OBJECTIONS: 'accepted_objections',
        UPLOAD_STATE_REJECTED: 'rejected',
    }.get(upload_state)


def resolve_unknown(query_ok: bool, received: bool | None, minutes_since_attempt: float | None,
                    wait_minutes: int) -> str:
    """Qué hacer con un envío sin confirmar según la consulta del documento."""
    if not query_ok or received is None:
        return RESOLVE_WAIT
    if received:
        return RESOLVE_RECEIVED
    # El SII dice que no lo tiene: se reenvía el mismo sobre, pero solo después de
    # un tiempo prudente (el SII puede tardar en registrar una carga reciente).
    if minutes_since_attempt is None or minutes_since_attempt >= wait_minutes:
        return RESOLVE_RESEND
    return RESOLVE_WAIT


def state_from_document_code(code: str | None) -> str | None:
    """Estado final cuando no hay Track ID y solo se puede consultar el documento.

    Criterio tf_dte_cl: un documento recibido con datos coincidentes (o ya
    modificado por una nota) se da por aceptado. DNK (datos no coinciden) y FAN
    (anulado) no son concluyentes y quedan para revisión con su detalle.
    """
    return 'accepted' if code in DOCUMENT_ACCEPTED_CODES else None


class TfDteClDocumentMixin(models.AbstractModel):
    _name = 'tf_dte_cl.document.mixin'
    _description = 'Ciclo de vida de un documento tributario electrónico'

    tf_dte_cl_document_type = fields.Selection(DTE_TYPES, string='Tipo DTE', readonly=True, copy=False)
    tf_dte_cl_folio = fields.Integer(string='Folio SII', readonly=True, copy=False, index=True)
    tf_dte_cl_caf_id = fields.Many2one('tf_dte_cl.caf', string='CAF', readonly=True, copy=False)
    tf_dte_cl_state = fields.Selection(
        DTE_STATES, string='Estado DTE', readonly=True, copy=False, index=True, tracking=True,
    )
    tf_dte_cl_error = fields.Text(string='Mensaje DTE', readonly=True, copy=False)
    tf_dte_cl_xml_file = fields.Binary(string='XML del DTE', attachment=True, readonly=True, copy=False)
    tf_dte_cl_xml_filename = fields.Char(readonly=True, copy=False)
    tf_dte_cl_ted = fields.Text(string='Timbre electrónico (TED)', readonly=True, copy=False)
    tf_dte_cl_barcode = fields.Binary(string='Timbre (PDF417)', attachment=True, readonly=True, copy=False)
    tf_dte_cl_signed_date = fields.Datetime(string='Firmado el', readonly=True, copy=False)
    tf_dte_cl_emission_date = fields.Date(string='Fecha de emisión DTE', readonly=True, copy=False)
    tf_dte_cl_receiver_rut = fields.Char(string='RUT receptor DTE', readonly=True, copy=False)
    tf_dte_cl_total_amount = fields.Integer(string='Monto total DTE', readonly=True, copy=False)
    tf_dte_cl_envelope_id = fields.Many2one('tf_dte_cl.envelope', string='Sobre', readonly=True, copy=False)
    tf_dte_cl_track_id = fields.Char(related='tf_dte_cl_envelope_id.track_id', string='Track ID')
    tf_dte_cl_sii_code = fields.Char(string='Código SII', readonly=True, copy=False)
    tf_dte_cl_sii_detail = fields.Text(string='Detalle SII', readonly=True, copy=False)
    tf_dte_cl_last_query = fields.Datetime(string='Última consulta SII', readonly=True, copy=False)

    _sql_constraints = [
        ('tf_dte_cl_folio_uniq', 'unique(company_id, tf_dte_cl_document_type, tf_dte_cl_folio)',
         'Ese folio ya fue asignado a otro documento.'),
    ]

    # ------------------------------------------------------------------
    # Métodos que implementa cada modelo concreto
    # ------------------------------------------------------------------
    def _tf_dte_cl_get_document_type(self) -> str | bool:
        """Código SII del documento ('33', '52', ...) o False si no es DTE."""
        raise NotImplementedError

    def _tf_dte_cl_get_branch(self):
        return self.env['tf_dte_cl.branch']

    def _tf_dte_cl_get_receiver(self):
        raise NotImplementedError

    def _tf_dte_cl_get_emission_date(self) -> date:
        raise NotImplementedError

    def _tf_dte_cl_get_references(self):
        raise NotImplementedError

    def _tf_dte_cl_specific_errors(self) -> list[str]:
        return []

    def _tf_dte_cl_document_values(self) -> dict:
        """Datos propios del documento.

        ``{'IdDoc': {...}, 'Transporte': {...}, 'Detalle': [...], 'TasaIVA': 19.0}``

        No incluye Folio, FchEmis, Receptor ni Referencia: los agrega el mixin.
        """
        raise NotImplementedError

    def _tf_dte_cl_line_infos(self) -> list:
        """Líneas de detalle (``dte_lines.LineInfo``)."""
        raise NotImplementedError

    def _tf_dte_cl_expected_amounts(self) -> dict:
        """Montos de Odoo a comparar con los calculados por la librería (``{'MntTotal': 119}``)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def _tf_dte_cl_client(self):
        return self.env['tf_dte_cl.sii.client']

    def _tf_dte_cl_label(self) -> str:
        self.ensure_one()
        if self.tf_dte_cl_folio:
            return 'T%sF%s' % (self.tf_dte_cl_document_type, self.tf_dte_cl_folio)
        return self.display_name

    def _tf_dte_cl_log(self, body: str) -> None:
        if hasattr(self, 'message_post'):
            for record in self:
                record.message_post(body=body)

    def _tf_dte_cl_base_payload(self) -> dict:
        self.ensure_one()
        company = self.company_id
        return {
            'Emisor': company._tf_dte_cl_emitter_payload(self._tf_dte_cl_get_branch()),
            'firma_electronica': company._tf_dte_cl_signature_payload(),
        }

    def _tf_dte_cl_receiver_payload(self) -> dict:
        self.ensure_one()
        partner = self._tf_dte_cl_get_receiver()
        commercial = partner.commercial_partner_id
        contact = partner.phone or partner.email or ''
        return {
            'RUTRecep': normalize_rut(commercial.vat),
            'RznSocRecep': commercial.name,
            'GiroRecep': commercial.tf_dte_cl_giro,
            'Contacto': contact[:80],
            'CorreoRecep': commercial.tf_dte_cl_dte_email or partner.email,
            'DirRecep': ', '.join(filter(None, [partner.street, partner.street2])),
            'CmnaRecep': partner.tf_dte_cl_comuna_id.name,
            'CiudadRecep': partner.city,
        }

    @staticmethod
    def _tf_dte_cl_commit(env) -> None:
        if not env.registry.in_test_mode():
            env.cr.commit()

    # ------------------------------------------------------------------
    # Validación previa
    # ------------------------------------------------------------------
    def _tf_dte_cl_readiness_errors(self) -> list[str]:
        self.ensure_one()
        _ = self.env._
        doc_type = self.tf_dte_cl_document_type or self._tf_dte_cl_get_document_type()
        if not doc_type:
            return [_('el documento no tiene un tipo DTE configurado')]
        errors = list(self.company_id._tf_dte_cl_emitter_errors())
        if not self.tf_dte_cl_folio:
            reason = self.env['tf_dte_cl.caf']._tf_dte_cl_unavailability_reason(self.company_id, doc_type)
            if reason:
                errors.append(reason)
        receiver = self._tf_dte_cl_get_receiver()
        if not receiver:
            errors.append(_('falta el cliente'))
        else:
            errors += [
                _('cliente "%(name)s": %(error)s', name=receiver.display_name, error=error)
                for error in receiver._tf_dte_cl_receiver_errors()
            ]
        emission = self._tf_dte_cl_get_emission_date()
        if not emission or not DATE_MIN <= emission <= DATE_MAX:
            errors.append(_('la fecha de emisión no es válida'))
        errors += self._tf_dte_cl_get_references()._tf_dte_cl_errors(doc_type)
        errors += self._tf_dte_cl_specific_errors()
        return errors

    def _tf_dte_cl_check_ready(self) -> None:
        for record in self:
            errors = record._tf_dte_cl_readiness_errors()
            if errors:
                raise UserError(self.env._(
                    'No se puede emitir "%(doc)s":\n- %(errors)s',
                    doc=record.display_name, errors='\n- '.join(errors),
                ))

    def _tf_dte_cl_check_amounts(self, amounts: dict) -> None:
        self.ensure_one()
        differences = []
        for key, expected in self._tf_dte_cl_expected_amounts().items():
            computed = round_half_up(amounts.get(key) or 0)
            expected = round_half_up(expected or 0)
            if computed != expected:
                differences.append('%s: DTE %s / Odoo %s' % (key, computed, expected))
        if differences:
            raise UserError(self.env._(
                'Los montos calculados para el DTE no coinciden con los del documento (%s). '
                'Revise el redondeo de impuestos y los precios de las líneas.',
                '; '.join(differences),
            ))

    # ------------------------------------------------------------------
    # Etapa 1: folio, firma y sobre (síncrono, sin red)
    # ------------------------------------------------------------------
    def _tf_dte_cl_full_document(self, folio: int) -> OrderedDict:
        self.ensure_one()
        values = self._tf_dte_cl_document_values()
        id_doc = OrderedDict(Folio=folio, FchEmis=self._tf_dte_cl_get_emission_date())
        id_doc.update(values.get('IdDoc') or {})
        header = OrderedDict(IdDoc=id_doc, Receptor=self._tf_dte_cl_receiver_payload())
        if values.get('Transporte'):
            header['Transporte'] = values['Transporte']
        doc_type = self.tf_dte_cl_document_type
        document = OrderedDict(NroDTE=1)
        if values.get('TasaIVA'):
            # La librería asigna TasaIVA antes que las líneas (priorizar) y la usa para el código 14.
            document['TasaIVA'] = values['TasaIVA']
        document['Encabezado'] = header
        document['Detalle'] = values.get('Detalle') or []
        references = self._tf_dte_cl_get_references()._tf_dte_cl_payload(doc_type)
        if references:
            document['Referencia'] = references
        return document

    def _tf_dte_cl_sign(self) -> None:
        """Toma el folio (si falta), timbra y firma. No hace nada si ya está firmado."""
        self.ensure_one()
        if self.tf_dte_cl_xml_file or self.tf_dte_cl_state in FINAL_STATES:
            return
        self._tf_dte_cl_check_ready()
        doc_type = self.tf_dte_cl_document_type or self._tf_dte_cl_get_document_type()
        base = self._tf_dte_cl_base_payload()
        with self.env.cr.savepoint():
            if self.tf_dte_cl_folio:
                caf, folio = self.tf_dte_cl_caf_id, self.tf_dte_cl_folio
            else:
                caf, folio = self.env['tf_dte_cl.caf']._tf_dte_cl_take_folio(self.company_id, doc_type)
                self.write({
                    'tf_dte_cl_document_type': doc_type,
                    'tf_dte_cl_folio': folio,
                    'tf_dte_cl_caf_id': caf.id,
                })
            payload = dict(base, Documento=[{
                'TipoDTE': int(doc_type),
                'caf_file': caf._tf_dte_cl_library_payload(),
                'documentos': [self._tf_dte_cl_full_document(folio)],
            }])
            result = self._tf_dte_cl_client().tf_dte_cl_sign(payload)[0]
            if not result.ok:
                raise UserError(self.env._('Error al firmar %(doc)s: %(msg)s', doc=self._tf_dte_cl_label(),
                                           msg=result.message))
            self._tf_dte_cl_check_amounts(result.amounts)
            receiver = self._tf_dte_cl_get_receiver().commercial_partner_id
            self.write({
                'tf_dte_cl_xml_file': base64.b64encode(
                    result.xml.encode('ISO-8859-1', errors='xmlcharrefreplace')
                ),
                'tf_dte_cl_xml_filename': 'DTE_%s.xml' % self._tf_dte_cl_label(),
                'tf_dte_cl_ted': result.ted,
                'tf_dte_cl_barcode': result.barcode_png_b64 or False,
                'tf_dte_cl_signed_date': fields.Datetime.now(),
                'tf_dte_cl_emission_date': self._tf_dte_cl_get_emission_date(),
                'tf_dte_cl_receiver_rut': normalize_rut(receiver.vat),
                'tf_dte_cl_total_amount': round_half_up(result.amounts.get('MntTotal') or 0),
                'tf_dte_cl_state': 'signed',
                'tf_dte_cl_error': False,
            })

    def _tf_dte_cl_pack(self) -> None:
        """Arma y firma el sobre con el DTE ya firmado. No hace nada si ya existe."""
        self.ensure_one()
        envelope = self.tf_dte_cl_envelope_id
        if envelope and envelope.state in ('draft', 'sent'):
            return
        if not self.tf_dte_cl_xml_file:
            raise UserError(self.env._('%s no tiene un DTE firmado.', self._tf_dte_cl_label()))
        envelope = self.env['tf_dte_cl.envelope'].sudo().create({
            'company_id': self.company_id.id,
            'res_model': self._name,
            'res_id': self.id,
            'document_type': self.tf_dte_cl_document_type,
            'folio': self.tf_dte_cl_folio,
        })
        filename = 'EnvioDTE_%s_%s.xml' % (self._tf_dte_cl_label(), envelope.id)
        payload = dict(
            self._tf_dte_cl_base_payload(),
            ID=envelope.name,
            filename=filename,
            Documento=[{
                'TipoDTE': int(self.tf_dte_cl_document_type),
                'documentos': [{
                    'NroDTE': 1,
                    'Folio': self.tf_dte_cl_folio,
                    'sii_xml_request': base64.b64decode(self.tf_dte_cl_xml_file).decode('ISO-8859-1'),
                }],
            }],
        )
        result = self._tf_dte_cl_client().tf_dte_cl_build_envelope(payload)
        if not result.ok:
            raise UserError(self.env._('Error al armar el sobre de %(doc)s: %(msg)s',
                                       doc=self._tf_dte_cl_label(), msg=result.message))
        envelope._tf_dte_cl_set_xml(result.xml, result.filename or filename)
        self.tf_dte_cl_envelope_id = envelope.id

    def _tf_dte_cl_prepare(self) -> None:
        """Etapa síncrona completa. Debe llamarse dentro de la transacción de publicación/validación."""
        for record in self:
            if not record._tf_dte_cl_get_document_type() or record.tf_dte_cl_state in FINAL_STATES:
                continue
            if not record.tf_dte_cl_state:
                record.tf_dte_cl_state = 'to_sign'
            record._tf_dte_cl_sign()
            record._tf_dte_cl_pack()

    # ------------------------------------------------------------------
    # Etapa 2: envío (red)
    # ------------------------------------------------------------------
    def _tf_dte_cl_resend_minutes(self) -> int:
        value = self.env['ir.config_parameter'].sudo().get_param('tf_dte_cl.unknown_resend_minutes')
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return DEFAULT_RESEND_MINUTES

    def _tf_dte_cl_send(self) -> str:
        """Envía el sobre guardado. Devuelve el estado DTE resultante."""
        self.ensure_one()
        if self.tf_dte_cl_state in FINAL_STATES or self.tf_dte_cl_state == 'sent':
            return self.tf_dte_cl_state
        envelope = self.tf_dte_cl_envelope_id.sudo()
        if not envelope or envelope.state != 'draft':
            self._tf_dte_cl_prepare()
            envelope = self.tf_dte_cl_envelope_id.sudo()
        if envelope._tf_dte_cl_unconfirmed_attempts():
            return self._tf_dte_cl_resolve_unknown()

        payload = dict(
            self._tf_dte_cl_base_payload(),
            ID=envelope.name,
            filename=envelope.xml_filename,
            sii_xml_request=envelope._tf_dte_cl_xml_text(),
        )
        envelope._tf_dte_cl_register_attempt()
        result = self._tf_dte_cl_client().tf_dte_cl_send_envelope(payload)
        envelope._tf_dte_cl_record_send(result)

        state = state_after_send(result.outcome)
        vals = {'tf_dte_cl_state': state, 'tf_dte_cl_error': result.message or False}
        if result.outcome == SEND_FAILED:
            # El SII no recibió el documento: se vuelve a firmar con el mismo folio.
            vals.update({'tf_dte_cl_xml_file': False, 'tf_dte_cl_ted': False, 'tf_dte_cl_barcode': False})
        self.write(vals)
        if result.outcome == SEND_OK:
            self._tf_dte_cl_log(self.env._('DTE %(doc)s enviado al SII (Track ID %(track)s).',
                                           doc=self._tf_dte_cl_label(), track=result.track_id))
        return state

    def _tf_dte_cl_query_document_payload(self) -> dict:
        self.ensure_one()
        return dict(self._tf_dte_cl_base_payload(), Documento=[{
            'TipoDTE': int(self.tf_dte_cl_document_type),
            'documentos': [{
                'Folio': self.tf_dte_cl_folio,
                'FchEmis': self.tf_dte_cl_emission_date,
                'Receptor': {'RUTRecep': self.tf_dte_cl_receiver_rut},
                'MntTotal': self.tf_dte_cl_total_amount,
            }],
        }])

    def _tf_dte_cl_resolve_unknown(self) -> str:
        self.ensure_one()
        envelope = self.tf_dte_cl_envelope_id.sudo()
        result = self._tf_dte_cl_client().tf_dte_cl_query_document(self._tf_dte_cl_query_document_payload())
        envelope._tf_dte_cl_record_query(result)
        _count, last_attempt = envelope._tf_dte_cl_attempt_stats()
        minutes = None
        if last_attempt:
            minutes = (fields.Datetime.now() - last_attempt).total_seconds() / 60
        decision = resolve_unknown(result.ok, result.received, minutes, self._tf_dte_cl_resend_minutes())
        vals = {'tf_dte_cl_last_query': fields.Datetime.now()}
        if result.ok:
            vals.update({'tf_dte_cl_sii_code': result.code, 'tf_dte_cl_sii_detail': result.detail or False})

        if decision == RESOLVE_WAIT:
            vals['tf_dte_cl_state'] = 'send_unknown'
            self.write(vals)
            return 'send_unknown'
        envelope._tf_dte_cl_confirm_attempts()
        if decision == RESOLVE_RECEIVED:
            envelope.write({'state': 'sent', 'date_sent': last_attempt})
            vals.update({
                'tf_dte_cl_state': state_from_document_code(result.code) or 'sent',
                'tf_dte_cl_error': False,
            })
            self.write(vals)
            return vals['tf_dte_cl_state']
        # RESOLVE_RESEND: el SII confirmó que no lo tiene; se reenvía el mismo sobre.
        vals['tf_dte_cl_state'] = 'signed'
        self.write(vals)
        return self._tf_dte_cl_send()

    # ------------------------------------------------------------------
    # Etapa 3: consulta (red)
    # ------------------------------------------------------------------
    def _tf_dte_cl_query(self) -> str:
        self.ensure_one()
        if self.tf_dte_cl_state == 'send_unknown':
            return self._tf_dte_cl_send()
        if self.tf_dte_cl_state != 'sent':
            return self.tf_dte_cl_state
        client = self._tf_dte_cl_client()
        envelope = self.tf_dte_cl_envelope_id.sudo()
        vals = {'tf_dte_cl_last_query': fields.Datetime.now()}
        if envelope.track_id:
            result = client.tf_dte_cl_query_upload(dict(self._tf_dte_cl_base_payload(),
                                                        codigo_envio=envelope.track_id))
            new_state = state_from_upload(result.state) if result.ok else None
        else:
            result = client.tf_dte_cl_query_document(self._tf_dte_cl_query_document_payload())
            new_state = state_from_document_code(result.code) if result.ok else None
        envelope._tf_dte_cl_record_query(result)
        if result.ok:
            vals.update({'tf_dte_cl_sii_code': result.code, 'tf_dte_cl_sii_detail': result.detail or False})
        if new_state:
            vals['tf_dte_cl_state'] = new_state
            vals['tf_dte_cl_error'] = False
        self.write(vals)
        if new_state:
            self._tf_dte_cl_log(self.env._('Estado SII de %(doc)s: %(state)s. %(detail)s',
                                           doc=self._tf_dte_cl_label(),
                                           state=dict(DTE_STATES)[new_state],
                                           detail=result.detail or ''))
        return self.tf_dte_cl_state

    # ------------------------------------------------------------------
    # Impresión
    # ------------------------------------------------------------------
    def _tf_dte_cl_document_name(self) -> str:
        self.ensure_one()
        label = dict(DTE_TYPES).get(self.tf_dte_cl_document_type or self._tf_dte_cl_get_document_type(), '')
        return label.rsplit(' (', 1)[0]

    @api.model
    def _tf_dte_cl_format_rut(self, rut) -> str:
        rut = normalize_rut(rut)
        if not rut:
            return ''
        body, digit = rut.split('-')
        return '%s-%s' % ('{:,}'.format(int(body)).replace(',', '.'), digit)

    @api.model
    def _tf_dte_cl_format_amount(self, amount) -> str:
        return '$ ' + '{:,}'.format(round_half_up(amount or 0)).replace(',', '.')

    @api.model
    def _tf_dte_cl_format_qty(self, quantity) -> str:
        """1.0 → '1'; 2.5 → '2,5' (hasta 4 decimales, sin ceros sobrantes)."""
        text = ('%.4f' % (quantity or 0)).rstrip('0').rstrip('.')
        integer, _sep, decimals = text.partition('.')
        integer = '{:,}'.format(int(integer)).replace(',', '.')
        return integer + (',' + decimals if decimals else '')

    def _tf_dte_cl_print_totals(self) -> list[tuple[str, int]]:
        self.ensure_one()
        totals = compute_totals(self._tf_dte_cl_line_infos())
        rows = []
        if totals['net']:
            rows.append((self.env._('Monto neto'), totals['net']))
        if totals['exempt']:
            rows.append((self.env._('Monto exento'), totals['exempt']))
        for code, (rate, amount) in totals['taxes'].items():
            name = SII_TAXES.get(code, (code,))[0]
            rows.append(('%s %s%%' % (name, ('%g' % rate).replace('.', ',')), amount))
        rows.append((self.env._('Monto total'), totals['total']))
        return rows

    # Manual de muestras impresas SII v4.0: copia cedible en 33, 34 y 52 (las notas no la llevan).
    CEDIBLE_DOCUMENT_TYPES = ('33', '34', '52')

    def _tf_dte_cl_allows_cedible(self) -> bool:
        self.ensure_one()
        return self.tf_dte_cl_document_type in self.CEDIBLE_DOCUMENT_TYPES

    def _tf_dte_cl_print_copies(self) -> list[bool]:
        """Copias a imprimir: la tributaria y, si corresponde, la cedible."""
        self.ensure_one()
        copies = [False]
        if self.company_id.tf_dte_cl_print_cedible and self._tf_dte_cl_allows_cedible():
            copies.append(True)
        return copies

    def _tf_dte_cl_cedible_legend(self) -> str:
        self.ensure_one()
        return 'CEDIBLE CON SU FACTURA' if self.tf_dte_cl_document_type == '52' else 'CEDIBLE'

    def _tf_dte_cl_sii_office_label(self) -> str:
        self.ensure_one()
        office = (self.company_id.tf_dte_cl_sii_office or '').strip()
        if not office:
            return ''
        return office if office.upper().startswith('S.I.I') else 'S.I.I. - %s' % office

    @api.model
    def _tf_dte_cl_has_discount(self, lines) -> bool:
        return any(line.discount for line in lines)

    @api.model
    def _tf_dte_cl_line_discount(self, line) -> int:
        """Monto de descuento de la línea, como lo informa el DTE (DescuentoMonto)."""
        if not line.discount:
            return 0
        gross = round_half_up(Decimal(str(line.quantity)) * Decimal(str(round(line.price_unit, 4))))
        return max(gross - round_half_up(line.subtotal), 0)

    def _tf_dte_cl_resolution_text(self) -> str:
        self.ensure_one()
        company = self.company_id
        year = company.tf_dte_cl_resolution_date.year if company.tf_dte_cl_resolution_date else ''
        return self.env._('Res. %(number)s de %(year)s', number=company.tf_dte_cl_resolution_number or 0, year=year)

    # ------------------------------------------------------------------
    # Acciones de usuario
    # ------------------------------------------------------------------
    def action_tf_dte_cl_download_xml(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/tf_dte_cl/download/dte/%s/%s' % (self._name, self.id),
            'target': 'self',
        }

    def action_tf_dte_cl_query(self):
        for record in self.filtered(lambda r: r.tf_dte_cl_state in PENDING_QUERY_STATES):
            record._tf_dte_cl_query()
        return True

    def action_tf_dte_cl_retry(self):
        """Reintenta un documento en error con el mismo folio (el SII no lo recibió)."""
        for record in self:
            if record.tf_dte_cl_state != 'error':
                raise UserError(self.env._('Solo se pueden reintentar documentos en error.'))
            record._tf_dte_cl_prepare()
            record.tf_dte_cl_state = 'signed'
        return True

    def action_tf_dte_cl_reset(self):
        """Libera un documento rechazado para emitirlo con un folio nuevo.

        El folio anterior nunca se reutiliza: queda registrado para anulación.
        """
        if not self.env.user.has_group('tf_dte_cl.group_tf_dte_cl_manager'):
            raise AccessError(self.env._('No tiene permiso para restablecer documentos electrónicos.'))
        for record in self:
            if record.tf_dte_cl_state != 'rejected':
                raise UserError(self.env._('Solo se pueden restablecer documentos rechazados por el SII.'))
            record._tf_dte_cl_register_void(self.env._('Documento rechazado por el SII'))
            record._tf_dte_cl_clear()
            record._tf_dte_cl_log(self.env._('DTE restablecido; se emitirá con un folio nuevo.'))
        return True

    def _tf_dte_cl_register_void(self, reason: str) -> None:
        self.ensure_one()
        if self.tf_dte_cl_folio:
            self.env['tf_dte_cl.caf.void']._tf_dte_cl_register(
                self.company_id, self.tf_dte_cl_document_type, self.tf_dte_cl_folio, reason, record=self,
            )

    def _tf_dte_cl_clear(self) -> None:
        self.write({
            'tf_dte_cl_document_type': False,
            'tf_dte_cl_folio': False,
            'tf_dte_cl_caf_id': False,
            'tf_dte_cl_state': False,
            'tf_dte_cl_error': False,
            'tf_dte_cl_xml_file': False,
            'tf_dte_cl_xml_filename': False,
            'tf_dte_cl_ted': False,
            'tf_dte_cl_barcode': False,
            'tf_dte_cl_signed_date': False,
            'tf_dte_cl_envelope_id': False,
            'tf_dte_cl_sii_code': False,
            'tf_dte_cl_sii_detail': False,
        })

    def _tf_dte_cl_cancel_dte(self) -> None:
        """Llamado al cancelar o volver a borrador el documento de Odoo."""
        for record in self.filtered('tf_dte_cl_state'):
            state = record.tf_dte_cl_state
            if state in VALID_STATES:
                raise UserError(self.env._(
                    '%s fue aceptado por el SII: para dejarlo sin efecto emita una nota de crédito.',
                    record._tf_dte_cl_label(),
                ))
            if state in PENDING_QUERY_STATES:
                raise UserError(self.env._(
                    '%s ya se envió al SII; espere su resultado antes de modificarlo.',
                    record._tf_dte_cl_label(),
                ))
            if state == 'rejected':
                raise UserError(self.env._(
                    '%s fue rechazado por el SII; restablézcalo antes de modificarlo.',
                    record._tf_dte_cl_label(),
                ))
            if record.tf_dte_cl_folio and state != 'voided':
                # Hubo firma (o intento de envío fallido): el folio no se reutiliza.
                record._tf_dte_cl_register_void(self.env._('Documento anulado antes de su aceptación'))
            record._tf_dte_cl_clear()

    # ------------------------------------------------------------------
    # Crons (los modelos concretos los invocan desde su propio ir.cron)
    # ------------------------------------------------------------------
    def _tf_dte_cl_lock_skip(self):
        """Filtra los registros que otro proceso tiene bloqueados."""
        if not self:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            'SELECT id FROM "%s" WHERE id IN %%s FOR UPDATE SKIP LOCKED' % self._table,
            [tuple(self.ids)],
        )
        return self.browse(row[0] for row in self.env.cr.fetchall())

    def _tf_dte_cl_run_each(self, method_name: str) -> None:
        """Ejecuta un paso por registro, con commit individual para no perder resultados de red."""
        for record_id in self.ids:
            record = self.browse(record_id).exists()._tf_dte_cl_lock_skip()
            if not record:
                continue
            try:
                with self.env.cr.savepoint():
                    getattr(record, method_name)()
            except UserError as error:
                self.env.invalidate_all()
                record.write({'tf_dte_cl_error': str(error.args[0]) if error.args else str(error)})
            except Exception:  # noqa: BLE001 - un documento no debe detener el lote
                self.env.invalidate_all()
                _logger.exception('Error DTE en %s (%s)', record.display_name, method_name)
            self._tf_dte_cl_commit(self.env)

    @api.model
    def _tf_dte_cl_cron_query(self, limit: int = 80, method_name: str = '_tf_dte_cl_query') -> None:
        threshold = fields.Datetime.now() - timedelta(minutes=QUERY_INTERVAL_MINUTES)
        records = self.search([
            ('tf_dte_cl_state', 'in', list(PENDING_QUERY_STATES)),
            '|', ('tf_dte_cl_last_query', '=', False), ('tf_dte_cl_last_query', '<', threshold),
        ], order='tf_dte_cl_last_query asc nulls first, id', limit=limit)
        records._tf_dte_cl_run_each(method_name)
