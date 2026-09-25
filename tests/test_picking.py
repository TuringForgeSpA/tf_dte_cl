# -*- coding: utf-8 -*-
"""Guía de despacho (52): validación, valorización, firma e impresión.

Ruta real: tests/test_picking.py
"""
from odoo import Command
from odoo.tests import tagged

from .common import TfDteClCommon


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestPicking(TfDteClCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.picking_type = cls.env.ref('stock.picking_type_out')
        cls.picking_type.write({
            'tf_dte_cl_document_type': '52',
            'tf_dte_cl_transfer_type': '1',
            'tf_dte_cl_dispatch_type': '1',
        })
        cls.caf_52 = cls.create_caf('52', 1, 20)
        cls.goods = cls.env['product.product'].create({
            'name': 'Caja de prueba',
            'default_code': 'CJ1',
            'type': 'consu',
            'list_price': 5000,
            'taxes_id': [Command.set(cls.tax_iva.ids)],
        })

    def create_picking(self, quantity=3):
        source = self.picking_type.default_location_src_id
        destination = self.env.ref('stock.stock_location_customers')
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.picking_type.id,
            'partner_id': self.partner.id,
            'location_id': source.id,
            'location_dest_id': destination.id,
            'move_ids': [Command.create({
                'name': self.goods.name,
                'product_id': self.goods.id,
                'product_uom_qty': quantity,
                'product_uom': self.goods.uom_id.id,
                'location_id': source.id,
                'location_dest_id': destination.id,
            })],
        })
        picking.action_confirm()
        picking.move_ids.write({'quantity': quantity, 'picked': True})
        return picking

    def test_defaults_from_operation_type(self):
        picking = self.create_picking()
        self.assertTrue(picking.tf_dte_cl_is_dte)
        self.assertEqual((picking.tf_dte_cl_transfer_type, picking.tf_dte_cl_dispatch_type), ('1', '1'))
        self.assertEqual(picking.tf_dte_cl_dest_comuna_id, self.comuna)

    def test_validate_signs_guide_with_list_price(self):
        with self.fake_sii() as fake:
            picking = self.create_picking()
            picking.button_validate()
        self.assertEqual(picking.state, 'done')
        self.assertEqual((picking.tf_dte_cl_state, picking.tf_dte_cl_folio), ('signed', 1))
        self.assertEqual(picking.tf_dte_cl_title, 'Guía de despacho electrónica N° 1')
        document = fake.last_sign_payload['Documento'][0]['documentos'][0]
        item = document['Detalle'][0]
        self.assertEqual((item['QtyItem'], item['PrcItem'], item['MontoItem']), (3, 5000, 15000))

    def test_missing_destination_blocks(self):
        with self.fake_sii() as fake:
            picking = self.create_picking()
            picking.tf_dte_cl_dest_comuna_id = False
            with self.assertUserError('comuna'):
                picking.button_validate()
        self.assertEqual(fake.calls['sign'], 0)
        self.assertEqual(self.caf_52.next_folio, 1)

    def test_delivery_slip_prints_guide_with_cedible_copy(self):
        with self.fake_sii():
            picking = self.create_picking()
            picking.button_validate()
        html = self.env['ir.actions.report']._render_qweb_html('stock.report_deliveryslip', picking.ids)[0]
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn('Guía de despacho electrónica', html.replace('GUÍA', 'Guía'))
        self.assertIn('CEDIBLE CON SU FACTURA', html)
        self.assertEqual(html.count('<!DOCTYPE html>'), 1)

    def test_thermal_guide_shows_dispatch_and_destination(self):
        with self.fake_sii():
            picking = self.create_picking()
            picking.button_validate()
        html = self.env['ir.actions.report']._render_qweb_html('tf_dte_cl.report_picking_dte_thermal', picking.ids)[0]
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn('Despacho: <span>Por cuenta del receptor</span>', html)
        self.assertIn('Destino: <span>Av. Siempre Viva 742</span>', html)
        self.assertEqual(html.count('<!DOCTYPE html>'), 1)          # un solo contenedor HTML

    def test_internal_transfer_has_no_cedible_copy(self):
        with self.fake_sii():
            picking = self.create_picking()
            picking.tf_dte_cl_transfer_type = '5'
            picking.button_validate()
        html = self.env['ir.actions.report']._render_qweb_html('stock.report_deliveryslip', picking.ids)[0]
        html = html.decode() if isinstance(html, bytes) else html
        self.assertNotIn('CEDIBLE', html)
