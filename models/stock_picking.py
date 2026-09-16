# -*- coding: utf-8 -*-
"""Guía de despacho electrónica (52).

Ruta real: models/stock_picking.py

Fuentes: SII, "Formato Documentos Tributarios Electrónicos" v2.4.2
(IdDoc: TipoDespacho, IndTraslado, TpoImpresion; zona Transporte) y
facturacion_electronica 0.24.0 (documento.py, Transporte).

Criterios tf_dte_cl:
* Todas las guías van valorizadas: precio de la línea de venta, si no el precio
  de lista del producto y, en último caso, 1 (se deja constancia en el chatter).
* Los indicadores de traslado 7, 8 y 9 corresponden a exportación y quedan
  fuera del alcance.
* En un traslado interno sin contacto, el receptor es la propia compañía.
"""
from __future__ import annotations

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .dte_lines import LineInfo, build_detail, iva_rate, line_errors, split_amounts
from .res_partner import is_valid_rut, normalize_rut

GUIDE_DTE_TYPE = '52'
PICKING_CODES = ('outgoing', 'internal')

TRANSFER_TYPES = [
    ('1', 'Operación constituye venta'),
    ('2', 'Ventas por efectuar'),
    ('3', 'Consignaciones'),
    ('4', 'Entrega gratuita'),
    ('5', 'Traslado interno'),
    ('6', 'Otros traslados no venta'),
]
INTERNAL_TRANSFER = '5'

DISPATCH_TYPES = [
    ('1', 'Por cuenta del receptor'),
    ('2', 'Por cuenta del emisor a instalaciones del cliente'),
    ('3', 'Por cuenta del emisor a otras instalaciones'),
]
DISPATCH_BY_ISSUER_OTHER = '3'

MAX_DEST_ADDRESS = 70   # DirDest
MAX_PLATE = 8           # Patente
MAX_DRIVER_NAME = 30    # NombreChofer
FALLBACK_PRICE = 1.0


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    tf_dte_cl_document_type = fields.Selection(
        [(GUIDE_DTE_TYPE, 'Guía de despacho electrónica (52)')], string='Tipo DTE',
    )
    tf_dte_cl_branch_id = fields.Many2one(
        'tf_dte_cl.branch', string='Sucursal SII', check_company=True,
        domain="[('company_id', '=', company_id)]",
    )
    tf_dte_cl_transfer_type = fields.Selection(
        TRANSFER_TYPES, string='Indicador de traslado por defecto',
    )
    tf_dte_cl_dispatch_type = fields.Selection(
        DISPATCH_TYPES, string='Tipo de despacho por defecto',
    )

    @api.constrains('tf_dte_cl_document_type', 'code')
    def _check_tf_dte_cl_document_type(self):
        for picking_type in self:
            if picking_type.tf_dte_cl_document_type and picking_type.code not in PICKING_CODES:
                raise ValidationError(self.env._(
                    'Solo las entregas y los traslados internos pueden emitir guías de despacho.'
                ))


class StockPicking(models.Model):
    _inherit = ['stock.picking', 'tf_dte_cl.document.mixin']

    tf_dte_cl_is_dte = fields.Boolean(string='Emite guía', compute='_compute_tf_dte_cl_is_dte')
    tf_dte_cl_reference_ids = fields.One2many(
        'tf_dte_cl.reference', 'picking_id', string='Referencias SII', copy=False,
    )
    tf_dte_cl_transfer_type = fields.Selection(
        TRANSFER_TYPES, string='Indicador de traslado',
        compute='_compute_tf_dte_cl_defaults', store=True, readonly=False,
    )
    tf_dte_cl_dispatch_type = fields.Selection(
        DISPATCH_TYPES, string='Tipo de despacho',
        compute='_compute_tf_dte_cl_defaults', store=True, readonly=False,
    )
    tf_dte_cl_carrier_id = fields.Many2one(
        'res.partner', string='Transportista', domain="[('tf_dte_cl_is_carrier', '=', True)]",
    )
    tf_dte_cl_vehicle_plate = fields.Char(string='Patente', size=MAX_PLATE)
    tf_dte_cl_driver_rut = fields.Char(string='RUT del chofer')
    tf_dte_cl_driver_name = fields.Char(string='Nombre del chofer', size=MAX_DRIVER_NAME)
    tf_dte_cl_dest_street = fields.Char(
        string='Dirección de destino', compute='_compute_tf_dte_cl_destination', store=True, readonly=False,
    )
    tf_dte_cl_dest_comuna_id = fields.Many2one(
        'tf_dte_cl.comuna', string='Comuna de destino',
        compute='_compute_tf_dte_cl_destination', store=True, readonly=False,
    )
    tf_dte_cl_dest_city = fields.Char(
        string='Ciudad de destino', compute='_compute_tf_dte_cl_destination', store=True, readonly=False,
    )

    @api.depends('picking_type_id.tf_dte_cl_document_type', 'picking_type_id.code')
    def _compute_tf_dte_cl_is_dte(self):
        for picking in self:
            picking.tf_dte_cl_is_dte = bool(
                picking.picking_type_id.tf_dte_cl_document_type
                and picking.picking_type_id.code in PICKING_CODES
            )

    @api.depends('picking_type_id')
    def _compute_tf_dte_cl_defaults(self):
        for picking in self:
            picking_type = picking.picking_type_id
            if not picking.tf_dte_cl_transfer_type:
                picking.tf_dte_cl_transfer_type = picking_type.tf_dte_cl_transfer_type
            if not picking.tf_dte_cl_dispatch_type:
                picking.tf_dte_cl_dispatch_type = picking_type.tf_dte_cl_dispatch_type

    @api.depends('partner_id')
    def _compute_tf_dte_cl_destination(self):
        for picking in self:
            partner = picking.partner_id
            picking.tf_dte_cl_dest_street = ', '.join(filter(None, [partner.street, partner.street2])) or False
            picking.tf_dte_cl_dest_comuna_id = partner.tf_dte_cl_comuna_id
            picking.tf_dte_cl_dest_city = partner.city

    # ------------------------------------------------------------------
    # Implementación del mixin
    # ------------------------------------------------------------------
    def _tf_dte_cl_get_document_type(self):
        self.ensure_one()
        return self.tf_dte_cl_is_dte and GUIDE_DTE_TYPE

    def _tf_dte_cl_get_branch(self):
        return self.picking_type_id.tf_dte_cl_branch_id

    def _tf_dte_cl_get_receiver(self):
        if self.partner_id:
            return self.partner_id
        if self.tf_dte_cl_transfer_type == INTERNAL_TRANSFER:
            return self.company_id.partner_id
        return self.env['res.partner']

    def _tf_dte_cl_get_emission_date(self):
        if self.date_done:
            return fields.Date.context_today(self, self.date_done)
        return fields.Date.context_today(self)

    def _tf_dte_cl_get_references(self):
        return self.tf_dte_cl_reference_ids

    def _tf_dte_cl_moves(self):
        return self.move_ids.filtered(lambda move: move.picked and move.quantity > 0)

    def _tf_dte_cl_move_price(self, move) -> tuple[float, float, models.Model, bool]:
        """(precio unitario en CLP y en la UdM del movimiento, descuento %, impuestos, precio de respaldo)."""
        company = self.company_id
        date = self._tf_dte_cl_get_emission_date()
        sale_line = move.sale_line_id
        if sale_line and sale_line.price_unit:
            price = sale_line.product_uom._compute_price(sale_line.price_unit, move.product_uom)
            if sale_line.currency_id != company.currency_id:
                price = sale_line.currency_id._convert(price, company.currency_id, company, date)
            tax_field = 'tax_id' if 'tax_id' in sale_line._fields else 'tax_ids'
            return price, sale_line.discount, sale_line[tax_field], False
        product = move.product_id
        taxes = product.taxes_id.filtered(lambda tax: tax.company_id == company)
        price = product.uom_id._compute_price(product.lst_price, move.product_uom)
        if price:
            return price, 0.0, taxes, False
        return FALLBACK_PRICE, 0.0, taxes, True

    def _tf_dte_cl_line_infos(self, with_fallback_flags: bool = False):
        currency = self.company_id.currency_id
        infos, fallback = [], []
        for move in self._tf_dte_cl_moves():
            price, discount, taxes, used_fallback = self._tf_dte_cl_move_price(move)
            subtotal = currency.round(move.quantity * price * (1 - (discount or 0.0) / 100.0))
            infos.append(LineInfo(
                label=move.product_id.display_name,
                product_name=move.product_id.name,
                description=move.description_picking or '',
                default_code=move.product_id.default_code or '',
                quantity=move.quantity,
                uom=move.product_uom.name or '',
                price_unit=price,
                discount=discount or 0.0,
                subtotal=subtotal,
                taxes=[tax._tf_dte_cl_info() for tax in taxes.sudo()],
            ))
            if used_fallback:
                fallback.append(move.product_id.display_name)
        return (infos, fallback) if with_fallback_flags else infos

    def _tf_dte_cl_specific_errors(self) -> list[str]:
        self.ensure_one()
        _ = self.env._
        errors = []
        if not self.tf_dte_cl_transfer_type:
            errors.append(_('falta el indicador de traslado'))
        if not self.tf_dte_cl_dispatch_type:
            errors.append(_('falta el tipo de despacho'))
        if self.tf_dte_cl_dispatch_type == DISPATCH_BY_ISSUER_OTHER:
            if not self.tf_dte_cl_carrier_id:
                errors.append(_('el despacho a otras instalaciones requiere el transportista'))
            elif not is_valid_rut(self.tf_dte_cl_carrier_id.commercial_partner_id.vat):
                errors.append(_('el transportista no tiene un RUT válido'))
        if self.tf_dte_cl_driver_rut:
            if not is_valid_rut(self.tf_dte_cl_driver_rut):
                errors.append(_('el RUT del chofer es inválido'))
            if not self.tf_dte_cl_driver_name:
                errors.append(_('si informa el RUT del chofer, debe indicar su nombre'))
        if not self.tf_dte_cl_dest_comuna_id:
            errors.append(_('falta la comuna de destino'))
        if not self.tf_dte_cl_dest_street:
            errors.append(_('falta la dirección de destino'))
        errors += line_errors(GUIDE_DTE_TYPE, self._tf_dte_cl_line_infos())
        return errors

    def _tf_dte_cl_document_values(self) -> dict:
        self.ensure_one()
        infos, fallback = self._tf_dte_cl_line_infos(with_fallback_flags=True)
        if fallback:
            self._tf_dte_cl_log(self.env._(
                'Guía valorizada con precio 1 por no tener precio de venta ni de lista: %s.',
                ', '.join(fallback),
            ))
        thermal = self.company_id.tf_dte_cl_print_format == 'thermal'
        id_doc = {
            'TipoDespacho': int(self.tf_dte_cl_dispatch_type),
            'IndTraslado': int(self.tf_dte_cl_transfer_type),
            'TpoImpresion': 'T' if thermal else 'N',
        }
        transport = {
            'Patente': (self.tf_dte_cl_vehicle_plate or '')[:MAX_PLATE],
            'DirDest': (self.tf_dte_cl_dest_street or '')[:MAX_DEST_ADDRESS],
            'CmnaDest': self.tf_dte_cl_dest_comuna_id.name,
            'CiudadDest': self.tf_dte_cl_dest_city,
        }
        if self.tf_dte_cl_carrier_id:
            transport['RUTTrans'] = normalize_rut(self.tf_dte_cl_carrier_id.commercial_partner_id.vat)
        if self.tf_dte_cl_driver_rut:
            transport['RUTChofer'] = normalize_rut(self.tf_dte_cl_driver_rut)
            transport['NombreChofer'] = (self.tf_dte_cl_driver_name or '')[:MAX_DRIVER_NAME]
        values = {'IdDoc': id_doc, 'Transporte': transport, 'Detalle': build_detail(infos)}
        rate = iva_rate(infos)
        if rate:
            values['TasaIVA'] = rate
        return values

    def _tf_dte_cl_expected_amounts(self) -> dict:
        # La guía no genera asientos: se verifican las bases calculadas desde los movimientos.
        net, exempt = split_amounts(self._tf_dte_cl_line_infos())
        return {'MntNeto': net, 'MntExe': exempt}

    # ------------------------------------------------------------------
    # Flujo de inventario
    # ------------------------------------------------------------------
    def button_validate(self):
        # Validación temprana (sin folio) para no mover stock si la guía no se puede emitir.
        # Si aún no hay cantidades preparadas, Odoo las marca al validar: se revisa en _action_done.
        self.filtered(
            lambda p: p.tf_dte_cl_is_dte and not p.tf_dte_cl_state and any(p.move_ids.mapped('picked'))
        )._tf_dte_cl_check_ready()
        return super().button_validate()

    def _action_done(self):
        res = super()._action_done()
        # Firma local (sin red): un error revierte la validación y el folio vuelve al CAF.
        self.filtered(lambda p: p.state == 'done' and p.tf_dte_cl_is_dte)._tf_dte_cl_prepare()
        return res

    @api.ondelete(at_uninstall=False)
    def _unlink_except_tf_dte_cl(self):
        if self.filtered('tf_dte_cl_folio'):
            raise UserError(self.env._('No se puede eliminar una transferencia que tuvo folio SII.'))

    # ------------------------------------------------------------------
    # Crons
    # ------------------------------------------------------------------
    @api.model
    def _cron_tf_dte_cl_send(self, limit=50):
        pickings = self.search([('tf_dte_cl_state', '=', 'signed')], order='id', limit=limit)
        pickings._tf_dte_cl_run_each('_tf_dte_cl_send')

    @api.model
    def _cron_tf_dte_cl_query(self):
        self._tf_dte_cl_cron_query()
