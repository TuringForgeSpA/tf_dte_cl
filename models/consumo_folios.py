# -*- coding: utf-8 -*-
import collections
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from facturacion_electronica import facturacion_electronica as fe
except ImportError:
    fe = None
    _logger.warning(
        'No se pudo importar la librería "facturacion_electronica". '
        'Instálela con: pip install facturacion_electronica'
    )

# Resumen de Consumo de Folios (RCOF): documento mensual/diario obligatorio
# ante el SII para todo emisor de DTE. A diferencia del módulo anterior, aquí
# no se restringe a boletas: cubre el universo de documentos B2B del módulo
# (33, 34, 43, 56, 61).
LISTA_DTE_B2B = ('33', '34', '43', '56', '61')

ESTADOS_CF = [
    ('draft', 'Borrador'),
    ('NoEnviado', 'No enviado'),
    ('EnCola', 'En cola'),
    ('Enviado', 'Enviado'),
    ('EnProceso', 'En proceso'),
    ('Aceptado', 'Aceptado'),
    ('Rechazado', 'Rechazado'),
    ('Reparo', 'Aceptado con reparos'),
    ('Anulado', 'Anulado'),
]

ESTADOS_VALIDOS = ('draft', 'Rechazado', 'Anulado')


class ConsumoFolios(models.Model):
    _name = 'account.move.consumo_folios'
    _description = 'Resumen de consumo de folios'
    _order = 'fecha_inicio desc'

    @api.model
    def _get_default_config_dte(self):
        return self.env['config.dte'].search([('company_id', '=', self.env.company.id)], limit=1)

    name = fields.Char(string='Detalle', required=True)
    state = fields.Selection(ESTADOS_CF, string='Estado', index=True, readonly=True, default='draft', copy=False)
    move_ids = fields.Many2many('account.move')
    fecha_inicio = fields.Date(string='Fecha de inicio', default=fields.Date.context_today)
    fecha_final = fields.Date(string='Fecha final', default=fields.Date.context_today)
    correlativo = fields.Integer(string='Correlativo', invisible=True)
    sec_envio = fields.Integer(string='Secuencia de envío')
    total_neto = fields.Monetary(string='Total neto', store=True, compute='_compute_totales')
    total_iva = fields.Monetary(string='Total IVA', store=True, compute='_compute_totales')
    total_exento = fields.Monetary(string='Total exento', store=True, compute='_compute_totales')
    total = fields.Monetary(string='Monto total', store=True, compute='_compute_totales')
    total_documentos = fields.Integer(string='Total de documentos', store=True, compute='_compute_totales')
    config_dte_id = fields.Many2one('config.dte', string='Configuración DTE', default=_get_default_config_dte)
    company_id = fields.Many2one('res.company', string='Compañía', required=True, default=lambda self: self.env.company.id)
    date = fields.Date(string='Fecha', required=True, default=fields.Date.context_today)
    detalles = fields.One2many('account.move.consumo_folios.detalles', 'cf_id', string='Detalle por rango')
    impuestos = fields.One2many('account.move.consumo_folios.impuestos', 'cf_id', string='Detalle de impuestos')
    anulaciones = fields.One2many('account.move.consumo_folios.anulaciones', 'cf_id', string='Anulaciones')
    currency_id = fields.Many2one(
        'res.currency', string='Moneda', required=True, default=lambda self: self.env.company.currency_id,
    )
    sii_xml_request = fields.Many2one('xml.envio', string='Sobre de envío', readonly=True, copy=False)

    @api.depends('impuestos.monto_iva', 'impuestos.monto_exento', 'impuestos.monto_total', 'detalles.cantidad')
    def _compute_totales(self):
        for r in self:
            r.total_iva = sum(r.impuestos.mapped('monto_iva'))
            r.total_exento = sum(r.impuestos.mapped('monto_exento'))
            r.total = sum(r.impuestos.mapped('monto_total'))
            r.total_neto = r.total - r.total_iva - r.total_exento
            r.total_documentos = sum(
                d.cantidad for d in r.detalles if d.tipo_operacion == 'utilizados'
            )

    def _get_moves(self):
        self.ensure_one()
        return self.with_context(lang='es_CL').move_ids.filtered(
            lambda m: m.is_invoice() and m.journal_id.cod_dte in LISTA_DTE_B2B and self._dte_folio(m.name)
        )

    def _dte_folio(self, nombre):
        digitos = ''.join(c for c in (nombre or '') if c.isdigit())
        return int(digitos) if digitos else False

    def _get_datos(self):
        grupos = {}
        for move in self._get_moves():
            cod_dte = int(move.journal_id.cod_dte)
            grupos.setdefault(cod_dte, []).append(move.with_context(tax_detail=True)._dte_build_consulta())
        for anulacion in self.anulaciones:
            cod_dte = int(anulacion.tpo_doc.codigo)
            for folio in range(anulacion.rango_inicio, anulacion.rango_final + 1):
                grupos.setdefault(cod_dte, []).append({
                    'Encabezado': {
                        'IdDoc': {'Folio': folio, 'FechaEmis': self.fecha_inicio.strftime('%d-%m-%Y'), 'Anulado': True},
                    },
                })
        return grupos

    def copy(self, default=None):
        raise UserError(_('No se puede duplicar un resumen de consumo de folios.'))

    def unlink(self):
        bloqueados = self.filtered(lambda r: r.state not in ESTADOS_VALIDOS)
        if bloqueados:
            raise UserError(_('No puede eliminar un resumen de consumo de folios ya transmitido.'))
        return super().unlink()

    def _emisor(self, conf):
        return self.env['account.move']._dte_data_emisor(conf) if conf else {}

    def _get_datos_empresa(self):
        self.ensure_one()
        if not self.config_dte_id:
            raise UserError(_('Debe seleccionar una configuración DTE.'))
        return {
            'Emisor': self._emisor(self.config_dte_id),
            'firma_electronica': self.env['account.move']._dte_data_firma_electronica(self.config_dte_id),
        }

    def action_validar(self):
        self.ensure_one()
        if fe is None:
            raise UserError(_('La librería "facturacion_electronica" no está instalada en el servidor.'))
        datos = self._get_datos_empresa()
        datos['ConsumoFolios'] = [self._get_datos()]
        resultado = fe.consumo_folios(datos)[0]
        doc_id = '%s_%s' % (self.fecha_inicio, self.sec_envio)
        self.sii_xml_request = self.env['xml.envio'].create({
            'sii_xml_request': resultado['sii_xml_request'],
            'name': doc_id,
            'company_id': self.company_id.id,
        }).id
        self.state = 'NoEnviado'

    def action_enviar(self):
        self.ensure_one()
        if fe is None:
            raise UserError(_('La librería "facturacion_electronica" no está instalada en el servidor.'))
        if not self.sii_xml_request:
            self.action_validar()
        datos = self._get_datos_empresa()
        datos.update({
            'sii_xml_request': self.sii_xml_request.sii_xml_request,
            'filename': self.sii_xml_request.name,
            'api': False,
        })
        respuesta = fe.enviar_xml(datos)
        self.sii_xml_request.write({
            'state': respuesta.get('status', 'NoEnviado'),
            'sii_send_ident': respuesta.get('sii_send_ident', ''),
            'sii_xml_response': respuesta.get('sii_xml_response', ''),
        })
        self.state = respuesta.get('status', 'NoEnviado')

    def action_consultar_estado(self):
        self.ensure_one()
        if fe is None:
            raise UserError(_('La librería "facturacion_electronica" no está instalada en el servidor.'))
        datos = self._get_datos_empresa()
        datos.update({'codigo_envio': self.sii_xml_request.sii_send_ident, 'api': False})
        respuesta = fe.consulta_estado_dte(datos)
        self.sii_xml_request.write({
            'state': respuesta.get('status'),
            'sii_receipt': respuesta.get('xml_resp', False),
        })
        self.state = 'Aceptado' if respuesta.get('status') == 'Aceptado' else respuesta.get('status')


class DetalleConsumoFolios(models.Model):
    _name = 'account.move.consumo_folios.detalles'
    _description = 'Línea de detalle de consumo de folios'

    cf_id = fields.Many2one('account.move.consumo_folios', string='Consumo de folios', ondelete='cascade')
    tpo_doc = fields.Many2one('account.move.docs.sii', string='Tipo de documento')
    tipo_operacion = fields.Selection([('utilizados', 'Utilizados'), ('anulados', 'Anulados')], string='Operación')
    folio_inicio = fields.Integer(string='Folio inicio')
    folio_final = fields.Integer(string='Folio final')
    cantidad = fields.Integer(string='Cantidad emitida')


class DetalleImpuestosConsumoFolios(models.Model):
    _name = 'account.move.consumo_folios.impuestos'
    _description = 'Línea de impuestos de consumo de folios'

    cf_id = fields.Many2one('account.move.consumo_folios', string='Consumo de folios', ondelete='cascade')
    tpo_doc = fields.Many2one('account.move.docs.sii', string='Tipo de documento')
    impuesto = fields.Many2one('account.tax', string='Impuesto')
    cantidad = fields.Integer(string='Cantidad')
    monto_neto = fields.Monetary(string='Monto neto')
    monto_iva = fields.Monetary(string='Monto IVA')
    monto_exento = fields.Monetary(string='Monto exento')
    monto_total = fields.Monetary(string='Monto total')
    currency_id = fields.Many2one(
        'res.currency', string='Moneda', required=True, default=lambda self: self.env.company.currency_id,
    )


class AnulacionesConsumoFolios(models.Model):
    _name = 'account.move.consumo_folios.anulaciones'
    _description = 'Línea de anulación de folios'

    cf_id = fields.Many2one('account.move.consumo_folios', string='Consumo de folios', ondelete='cascade')
    tpo_doc = fields.Many2one(
        'account.move.docs.sii', string='Tipo de documento', required=True,
        domain=[('codigo', 'in', ['33', '34', '43', '56', '61'])],
    )
    rango_inicio = fields.Integer(string='Rango inicio', required=True)
    rango_final = fields.Integer(string='Rango final', required=True)
