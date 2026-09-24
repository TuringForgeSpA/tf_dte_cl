# -*- coding: utf-8 -*-
"""Sobres EnvioDTE y sus intentos de envío.

Ruta real: models/envelope.py

Reemplaza al antiguo modelo ``xml.envio``. Un sobre contiene un único DTE
(así los contadores de la consulta de envío son inequívocos).

Idempotencia: antes de cada envío se inserta un intento en una transacción
propia. Si el proceso se interrumpe después de enviar y antes de guardar el
resultado, el intento queda "sin confirmar" y el documento debe consultarse
al SII antes de reenviar.
"""
from __future__ import annotations

import base64

from odoo import api, fields, models
from odoo.exceptions import UserError

from .caf import DTE_TYPES
from .sii_client import SEND_FAILED, SEND_OK, SEND_RETRY, SEND_UNKNOWN

ENVELOPE_STATES = [
    ('draft', 'Firmado, sin enviar'),
    ('sent', 'Enviado'),
    ('failed', 'Carga rechazada'),
]


class TfDteClEnvelope(models.Model):
    _name = 'tf_dte_cl.envelope'
    _description = 'Sobre EnvioDTE'
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(string='Identificador', readonly=True, copy=False, index=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, index=True,
        default=lambda self: self.env.company,
    )
    res_model = fields.Char(string='Modelo de origen', readonly=True, index=True)
    res_id = fields.Many2oneReference(string='Documento de origen', model_field='res_model', readonly=True)
    document_type = fields.Selection(DTE_TYPES, string='Tipo de documento', readonly=True)
    folio = fields.Integer(string='Folio', readonly=True)
    state = fields.Selection(ENVELOPE_STATES, string='Estado', default='draft', required=True, readonly=True)
    xml_file = fields.Binary(string='XML del sobre', attachment=True, readonly=True, copy=False)
    xml_filename = fields.Char(string='Nombre del archivo', readonly=True)
    track_id = fields.Char(string='Track ID', readonly=True, index=True, copy=False)
    upload_status = fields.Char(string='STATUS de la carga', readonly=True)
    date_sent = fields.Datetime(string='Fecha de envío', readonly=True)
    last_message = fields.Text(string='Último mensaje', readonly=True)
    sii_code = fields.Char(string='Último código SII', readonly=True)
    sii_detail = fields.Text(string='Detalle SII', readonly=True)
    sii_response = fields.Text(string='Última respuesta SII', readonly=True)
    last_query = fields.Datetime(string='Última consulta', readonly=True)
    query_count = fields.Integer(string='Consultas', readonly=True)
    attempt_ids = fields.One2many('tf_dte_cl.envelope.attempt', 'envelope_id', string='Intentos', readonly=True)
    confirmed_attempts = fields.Integer(
        string='Intentos con resultado', readonly=True,
        help='Intentos cuyo resultado quedó guardado. Si hay más intentos que resultados, '
             'el último envío no está confirmado.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        envelopes = super().create(vals_list)
        for envelope in envelopes:
            # Debe ser un identificador XML válido: se usa como ID del SetDTE y URI de la firma.
            envelope.name = 'TFDTE%s' % envelope.id
        return envelopes

    def unlink(self):
        if any(envelope.state == 'sent' or envelope.attempt_ids for envelope in self):
            raise UserError(self.env._('No se puede eliminar un sobre que ya se intentó enviar al SII.'))
        return super().unlink()

    # ------------------------------------------------------------------
    # XML
    # ------------------------------------------------------------------
    def _tf_dte_cl_set_xml(self, xml_text: str, filename: str) -> None:
        self.ensure_one()
        self.write({
            'xml_file': base64.b64encode(xml_text.encode('ISO-8859-1', errors='xmlcharrefreplace')),
            'xml_filename': filename,
        })

    def _tf_dte_cl_xml_text(self) -> str:
        self.ensure_one()
        if not self.xml_file:
            raise UserError(self.env._('El sobre %s no tiene XML.', self.name))
        return base64.b64decode(self.xml_file).decode('ISO-8859-1')

    # ------------------------------------------------------------------
    # Intentos
    # ------------------------------------------------------------------
    def _tf_dte_cl_attempt_stats(self) -> tuple[int, fields.Datetime | None]:
        """(intentos registrados, fecha del último) según la base de datos."""
        self.ensure_one()
        self.env.cr.execute(
            'SELECT count(*), max(date) FROM tf_dte_cl_envelope_attempt WHERE envelope_id = %s',
            [self.id],
        )
        count, last = self.env.cr.fetchone()
        return count, last

    def _tf_dte_cl_unconfirmed_attempts(self) -> int:
        count, _last = self._tf_dte_cl_attempt_stats()
        return max(count - self.confirmed_attempts, 0)

    def _tf_dte_cl_register_attempt(self) -> None:
        """Registra el intento en una transacción propia, que sobrevive a un rollback.

        Si el sobre todavía no está confirmado en la base (se creó en esta misma
        transacción, por ejemplo al volver a firmar tras un envío fallido), otra
        conexión no lo ve y la llave foránea fallaría. En ese caso el intento se
        registra en la transacción actual: si esta se deshace, el sobre también
        desaparece y no hay intento que preservar.
        """
        self.ensure_one()
        self.flush_recordset()
        query = """
            INSERT INTO tf_dte_cl_envelope_attempt
                   (envelope_id, date, create_uid, create_date, write_uid, write_date)
            VALUES (%s, now() at time zone 'UTC', %s, now() at time zone 'UTC',
                    %s, now() at time zone 'UTC')
        """
        params = [self.id, self.env.uid, self.env.uid]
        with self.env.registry.cursor() as cr:
            cr.execute('SELECT 1 FROM tf_dte_cl_envelope WHERE id = %s', [self.id])
            if cr.fetchone():
                cr.execute(query, params)
                return
        self.env.cr.execute(query, params)

    def _tf_dte_cl_confirm_attempts(self) -> None:
        """Da por resueltos todos los intentos registrados hasta ahora."""
        self.ensure_one()
        count, _last = self._tf_dte_cl_attempt_stats()
        self.confirmed_attempts = count

    def _tf_dte_cl_record_send(self, result) -> None:
        """Guarda el resultado de un envío. Un resultado desconocido no confirma el intento."""
        self.ensure_one()
        vals = {'last_message': result.message or False, 'upload_status': result.upload_status or False}
        if result.outcome != SEND_UNKNOWN:
            vals['confirmed_attempts'] = self.confirmed_attempts + 1
        if result.outcome == SEND_OK:
            vals.update({'state': 'sent', 'track_id': result.track_id, 'date_sent': fields.Datetime.now()})
        elif result.outcome == SEND_FAILED:
            vals['state'] = 'failed'
        elif result.outcome == SEND_RETRY:
            vals['state'] = 'draft'
        self.write(vals)

    def _tf_dte_cl_record_query(self, result) -> None:
        self.ensure_one()
        vals = {'last_query': fields.Datetime.now(), 'query_count': self.query_count + 1}
        if result.ok:
            vals.update({
                'sii_code': result.code,
                'sii_detail': result.detail or False,
                'sii_response': result.raw or False,
            })
        else:
            vals['last_message'] = result.detail or False
        self.write(vals)

    def action_open_document(self):
        self.ensure_one()
        if not self.res_model or not self.res_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
        }


class TfDteClEnvelopeAttempt(models.Model):
    _name = 'tf_dte_cl.envelope.attempt'
    _description = 'Intento de envío de un sobre'
    _order = 'date desc, id desc'

    envelope_id = fields.Many2one(
        'tf_dte_cl.envelope', string='Sobre', required=True, ondelete='restrict', index=True,
    )
    date = fields.Datetime(string='Fecha', required=True, default=fields.Datetime.now)
