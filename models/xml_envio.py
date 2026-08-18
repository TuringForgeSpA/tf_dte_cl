# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError
from io import BytesIO

import pdf417gen
import base64
import logging

# NOTA: se eliminó "from .facturacion_electronica import facturacion_electronica as fe".
# La librería ahora se instala vía pip y su único consumidor es
# account_edi_format.py (importación defensiva). Mantener aquí el import
# del paquete vendorizado impediría cargar el módulo, porque esa carpeta
# ya no existe. También se quitaron los imports de lxml.etree y json, que
# solo usaba el método consulta_estado_dte() ya eliminado.

_logger = logging.getLogger(__name__)

status_dte = [
    ("no_revisado", "No Revisado"),
    ("0", "Conforme"),
    ("1", "Error de Schema"),
    ("2", "Error de Firma"),
    ("3", "RUT Receptor No Corresponde"),
    ("90", "Archivo Repetido"),
    ("91", "Archivo Ilegible"),
    ("99", "Envio Rechazado - Otros"),
]


class XMLEnvio(models.Model):
    """Registro de auditoría del envío al SII.

    IMPORTANTE: este modelo ya NO es la fuente de verdad del estado del
    DTE. El ciclo de vida (to_send -> sent, reintentos, errores) lo
    gobierna account.edi.document a través del formato EDI 'sii_dte'.
    Aquí solo se conservan el XML crudo, el Track ID y el código de
    barras PDF417 que necesita la representación impresa.
    """
    _name = 'xml.envio'
    _description = 'XML de envío DTE'

    ESTADOS = [
        ("NoEnviado", "No Enviado"),
        ("Enviado", "Enviado"),
        ("EnProceso", "En Proceso"),
        ("Aceptado", "Aceptado"),
        # Aceptado con observaciones: el DTE es válido ante el SII, pero
        # trae reparos. Se agrega para poder reflejar el estado 'Reparo'
        # que devuelve _l10n_cl_query_dte_status sin caer en un
        # ValueError al escribir el Selection.
        ("Reparo", "Aceptado con Reparo"),
        ("Rechazado", "Rechazado"),
    ]

    name = fields.Char(string='Nombre de envío', copy=False)
    sii_xml_request = fields.Text(string='XML Envío', copy=False)
    state = fields.Selection(ESTADOS, default='NoEnviado', copy=False)
    # El string va sin _(): las traducciones no deben evaluarse en tiempo
    # de import; Odoo traduce las etiquetas de campo automáticamente.
    sii_barcode = fields.Char(copy=False, string='Código de barras')
    sii_barcode_img = fields.Binary(string='Código de barras (IMG)', copy=False)
    sii_xml_response = fields.Text(string='Respuesta XML', copy=False)
    sii_xml_dte = fields.Text(string='XML DTE', copy=False)
    sii_send_ident = fields.Char(string='Track ID', copy=False)
    sii_receipt = fields.Text(string='Glosa recepción', copy=False)
    move_id = fields.Many2one('account.move', string='Documento', copy=False)
    company_id = fields.Many2one('res.company', string='Compañía', default=lambda self: self.env.user.company_id.id)

    @api.model_create_multi
    def create(self, vals_list):
        """Genera la imagen PDF417 del timbre a partir del TED en texto.

        La versión anterior solo procesaba vals_list[0] y descartaba el
        resto del lote (vals_list = [vals]), lo que rompía cualquier
        creación múltiple. Ahora se recorre la lista completa.
        """
        for vals in vals_list:
            if vals.get('sii_barcode'):
                vals['sii_barcode_img'] = self.get_barcode_img(
                    columns=13, ratio=3, xml=vals['sii_barcode'],
                )
        return super(XMLEnvio, self).create(vals_list)

    def unlink(self):
        for r in self:
            if r.state in ['Aceptado', 'Reparo', 'Enviado', 'EnProceso']:
                raise UserError('No se puede eliminar un documento válido ante el SII')
        return super(XMLEnvio, self).unlink()

    def get_barcode_img(self, columns=13, ratio=3, xml=False):
        barcodefile = BytesIO()
        image = self.pdf417bc(xml, columns, ratio)
        image.save(barcodefile, 'PNG')
        data = barcodefile.getvalue()
        return base64.b64encode(data)

    def pdf417bc(self, ted, columns, ratio):
        bc = pdf417gen.encode(ted, security_level=5, columns=columns, encoding='ISO-8859-1',)
        image = pdf417gen.render_image(bc, padding=15, scale=1, ratio=ratio,)
        return image

    # ==========================================================
    # ELIMINADO: consulta_estado_dte()
    # ==========================================================
    # Delegaba en account.move.consulta_estado_dte(), que ya no existe.
    # La consulta del Track ID la hace ahora el cron del framework EDI:
    #   account.edi.format._l10n_cl_cron_update_dte_status()
    #     -> _l10n_cl_update_dte_status(document)
    #        -> _l10n_cl_query_dte_status(move, cod_dte)
    # que escribe aquí 'state', 'sii_receipt' y 'sii_xml_response'.
    # Si se quiere un botón de "consultar ahora" en la vista de xml.envio,
    # debe llamar a ese método del EDI, no reimplementar la consulta.
