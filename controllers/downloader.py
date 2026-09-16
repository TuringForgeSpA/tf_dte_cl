# -*- coding: utf-8 -*-
"""Descarga de XML de DTE y de sobres.

Ruta real: controllers/downloader.py

Se respetan los permisos del usuario: un registro inexistente o sin acceso
responde 404, sin revelar cuál de los dos casos ocurrió.
"""
import base64

from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import content_disposition, request

DTE_MODELS = ('account.move', 'stock.picking')


class TfDteClDownloader(http.Controller):

    def _xml_response(self, content: bytes, filename: str):
        headers = [
            ('Content-Type', 'application/xml; charset=ISO-8859-1'),
            ('Content-Disposition', content_disposition(filename)),
            ('X-Content-Type-Options', 'nosniff'),
        ]
        return request.make_response(content, headers=headers)

    def _readable(self, model: str, res_id: int):
        record = request.env[model].browse(res_id)
        try:
            record = record.exists()
            if not record:
                return None
            record.check_access('read')
        except (AccessError, MissingError):
            return None
        return record

    @http.route('/tf_dte_cl/download/envelope/<int:envelope_id>', type='http', auth='user')
    def download_envelope(self, envelope_id, **kwargs):
        envelope = self._readable('tf_dte_cl.envelope', envelope_id)
        if not envelope or not envelope.xml_file:
            return request.not_found()
        return self._xml_response(base64.b64decode(envelope.xml_file), envelope.xml_filename or '%s.xml' % envelope.name)

    @http.route('/tf_dte_cl/download/dte/<string:model>/<int:res_id>', type='http', auth='user')
    def download_dte(self, model, res_id, **kwargs):
        if model not in DTE_MODELS:
            return request.not_found()
        record = self._readable(model, res_id)
        if not record or not record.tf_dte_cl_xml_file:
            return request.not_found()
        return self._xml_response(
            base64.b64decode(record.tf_dte_cl_xml_file),
            record.tf_dte_cl_xml_filename or 'DTE_%s.xml' % record.id,
        )
