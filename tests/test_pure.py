# -*- coding: utf-8 -*-
"""Funciones puras: RUT, líneas e impuestos, referencias, CAF y decisiones del ciclo.

Ruta real: tests/test_pure.py
"""
from datetime import date

from odoo.tests import TransactionCase, tagged

from odoo.addons.tf_dte_cl.models import dte_lines as dl
from odoo.addons.tf_dte_cl.models import dte_mixin as mx
from odoo.addons.tf_dte_cl.models.caf import CafError, environment_from_idk, parse_caf
from odoo.addons.tf_dte_cl.models.reference import validate_references
from odoo.addons.tf_dte_cl.models.res_partner import is_valid_rut, normalize_rut, rut_check_digit

from .common import COMPANY_RUT, make_caf

IVA = dl.TaxInfo('IVA', '14', 19.0)
WINE = dl.TaxInfo('Vinos', '25', 20.5)


def line(label='A', qty=2, price=1000.0, discount=0.0, subtotal=None, taxes=(IVA,), **kw):
    subtotal = qty * price * (1 - discount / 100) if subtotal is None else subtotal
    return dl.LineInfo(label, 'Producto ' + label, quantity=qty, price_unit=price, discount=discount,
                       subtotal=subtotal, taxes=list(taxes), **kw)


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestRut(TransactionCase):

    def test_check_digit(self):
        self.assertEqual(rut_check_digit('60803000'), 'K')   # RUT del SII
        self.assertEqual(rut_check_digit('78479445'), '8')

    def test_valid_and_normalized(self):
        self.assertTrue(is_valid_rut('60.803.000-K'))
        self.assertTrue(is_valid_rut('60803000-k'))
        self.assertFalse(is_valid_rut('60803000-1'))
        self.assertFalse(is_valid_rut(''))
        self.assertEqual(normalize_rut('CL 76.086.428-5'), '76086428-5')


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestLines(TransactionCase):

    def test_line_rules(self):
        cases = [
            ('33', [line()], 0),
            ('33', [line(taxes=())], 1),                        # 33 sin IVA
            ('33', [line(), line('B', taxes=())], 0),           # afecta + exenta
            ('34', [line()], 1),                                # 34 con IVA
            ('34', [line(taxes=())], 0),
            ('33', [line(taxes=(WINE,)), line('B')], 1),        # adicional sin IVA
            ('33', [line(taxes=(IVA, WINE, dl.TaxInfo('Cerveza', '26', 20.5)))], 1),
            ('33', [line(taxes=(dl.TaxInfo('IVA incluido', '14', 19.0, price_include=True),))], 1),
            ('33', [line(taxes=(dl.TaxInfo('IVA 18', '14', 18.0),))], 1),
            ('33', [line(qty=0)], 1),
            ('33', [line(price=-5)], 1),
            ('61', [line(str(i)) for i in range(61)], 1),       # más de 60 líneas
        ]
        for doc_type, lines, expected in cases:
            with self.subTest(doc_type=doc_type, lines=len(lines)):
                self.assertEqual(len(dl.line_errors(doc_type, lines)), expected)

    def test_detail_discount_and_exempt(self):
        detail = dl.build_detail([
            line('A', qty=3, price=1234.5, discount=10, subtotal=3333.0, default_code='SKU-1', uom='Unidades'),
            line('B', qty=1, price=500, taxes=()),
        ])
        first, second = detail
        self.assertEqual(first['DescuentoMonto'], 371)          # 3704 - 3333
        self.assertEqual(first['UnmdItem'], 'Unid')
        self.assertEqual(first['Impuesto'], [{'CodImp': 14, 'TasaImp': 19.0}])
        self.assertEqual(second['IndExe'], 1)
        self.assertNotIn('Impuesto', second)

    def test_totals(self):
        totals = dl.compute_totals([
            line('A', qty=1, price=1001, subtotal=1001),
            line('B', qty=1, price=1002, subtotal=1002, taxes=(IVA, WINE)),
            line('C', qty=1, price=500, subtotal=500, taxes=()),
        ])
        self.assertEqual(totals['taxes']['14'][1], 381)          # 2003 x 19 %
        self.assertEqual(totals['taxes']['25'][1], 205)          # 1002 x 20,5 %
        self.assertEqual(totals['total'], 2003 + 500 + 381 + 205)


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestReferences(TransactionCase):

    def ref(self, **values):
        base = {'type_code': '33', 'type_kind': 'tax', 'folio': '10', 'is_global': False,
                'date': date(2026, 9, 1), 'other_rut': False, 'code': '1', 'reason': 'Anula'}
        base.update(values)
        return base

    def test_credit_note_needs_reference(self):
        self.assertTrue(validate_references('61', []))
        self.assertFalse(validate_references('61', [self.ref()]))

    def test_text_correction_format(self):
        errors = validate_references('61', [self.ref(code='2', reason='corrige el giro')])
        self.assertTrue(errors)
        self.assertFalse(validate_references('61', [self.ref(code='2', reason='DICE: A DEBE DECIR: B')]))


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestCafParsing(TransactionCase):

    def test_parse_valid(self):
        data = parse_caf(make_caf(COMPANY_RUT, '33', 1, 10, idk='300'))
        self.assertEqual((data['document_type'], data['folio_from'], data['folio_to']), ('33', 1, 10))
        self.assertEqual(environment_from_idk(data['idk']), 'produccion')

    def test_parse_rejects(self):
        with self.assertRaises(CafError):
            parse_caf(make_caf(COMPANY_RUT, '33', 1, 10, with_signature=False))
        with self.assertRaises(CafError):
            parse_caf(make_caf(COMPANY_RUT, '33', 10, 1))
        with self.assertRaises(CafError):
            parse_caf(b'<Foo/>')

    def test_environment_from_idk(self):
        self.assertEqual(environment_from_idk('100'), 'certificacion')
        self.assertEqual(environment_from_idk(' 300 '), 'produccion')
        self.assertFalse(environment_from_idk('200'))
        self.assertFalse(environment_from_idk(None))


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestCycleDecisions(TransactionCase):

    def test_resolve_unknown(self):
        self.assertEqual(mx.resolve_unknown(False, None, 90, 60), mx.RESOLVE_WAIT)
        self.assertEqual(mx.resolve_unknown(True, True, 1, 60), mx.RESOLVE_RECEIVED)
        self.assertEqual(mx.resolve_unknown(True, False, 10, 60), mx.RESOLVE_WAIT)
        self.assertEqual(mx.resolve_unknown(True, False, 61, 60), mx.RESOLVE_RESEND)
        self.assertEqual(mx.resolve_unknown(True, None, 90, 60), mx.RESOLVE_WAIT)

    def test_document_codes(self):
        self.assertEqual(mx.state_from_document_code('DOK'), 'accepted')
        self.assertIsNone(mx.state_from_document_code('FAN'))
        self.assertIsNone(mx.state_from_document_code(None))
