# -*- coding: utf-8 -*-
import base64
import logging
from io import BytesIO

import pdf417gen

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ESTADOS_XML_ENVIO = [
    ('NoEnviado', 'No enviado'),
    ('Enviado', 'Enviado'),
    ('EnProceso', 'En proceso'),
    ('Proceso', 'En proceso'),
    ('Aceptado', 'Aceptado'),
    ('Reparo', 'Aceptado con reparos'),
    ('Rechazado', 'Rechazado'),
]

ESTADOS_BLOQUEANTES = ('Aceptado', 'Reparo', 'Enviado', 'EnProceso', 'Proceso')


class XmlEnvio(models.Model):
    _name = 'xml.envio'
    _description = 'Sobre de envío DTE'

    name = fields.Char(string='Nombre de envío', copy=False)
    sii_xml_request = fields.Text(string='XML de envío', copy=False)
    state = fields.Selection(ESTADOS_XML_ENVIO, string='Estado', default='NoEnviado', copy=False)
    sii_barcode = fields.Char(string='Código de barras (TED)', copy=False)
    sii_barcode_img = fields.Binary(string='Código de barras (imagen)', copy=False)
    sii_xml_response = fields.Text(string='Respuesta del SII', copy=False)
    sii_xml_dte = fields.Text(string='XML del DTE', copy=False)
    sii_send_ident = fields.Char(string='Track ID', copy=False)
    sii_receipt = fields.Text(string='Glosa de recepción', copy=False)
    move_id = fields.Many2one('account.move', string='Documento contable', copy=False)
    pick_id = fields.Many2one('stock.picking', string='Guía de despacho', copy=False)
    company_id = fields.Many2one(
        'res.company', string='Compañía', default=lambda self: self.env.company.id,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('sii_barcode'):
                vals['sii_barcode_img'] = self._get_barcode_img(vals['sii_barcode'])
        return super().create(vals_list)

    def unlink(self):
        if self.filtered(lambda r: r.state in ESTADOS_BLOQUEANTES):
            raise UserError(_('No se puede eliminar un documento válido ante el SII.'))
        return super().unlink()

    def _get_barcode_img(self, ted, columns=13, ratio=3):
        buffer = BytesIO()
        self._pdf417_image(ted, columns, ratio).save(buffer, 'PNG')
        return base64.b64encode(buffer.getvalue())

    def _pdf417_image(self, ted, columns, ratio):
        barcode = pdf417gen.encode(ted, security_level=5, columns=columns, encoding='ISO-8859-1')
        return pdf417gen.render_image(barcode, padding=15, scale=1, ratio=ratio)

    def action_consultar_estado_dte(self):
        for envio in self:
            documento = envio.move_id or envio.pick_id
            if documento:
                documento.consulta_estado_dte()
        return True
