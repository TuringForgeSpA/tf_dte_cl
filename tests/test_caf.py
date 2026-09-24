# -*- coding: utf-8 -*-
"""CAF: carga, validaciones, ambiente y toma de folios.

Ruta real: tests/test_caf.py
"""
import base64

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import COMPANY_RUT, TfDteClCommon, make_caf, make_rut


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestCaf(TfDteClCommon):

    def test_loaded_values(self):
        caf = self.caf_33
        self.assertEqual((caf.document_type, caf.folio_from, caf.folio_to), ('33', 1, 50))
        self.assertEqual(caf.next_folio, 1)
        self.assertEqual(caf.environment, 'certificacion')
        self.assertTrue(caf.date_expiry)

    def test_rejects_other_company_rut(self):
        with self.assertRaises(UserError):
            self.env['tf_dte_cl.caf'].create({
                'company_id': self.company.id,
                'caf_file': base64.b64encode(make_caf(make_rut('11111111'), '34', 1, 10)),
            })

    def test_overlap_same_environment(self):
        self.create_caf('56', 1, 20)
        with self.assertRaises(ValidationError):
            self.create_caf('56', 10, 30)

    def test_overlap_allowed_between_environments(self):
        certification = self.create_caf('56', 1, 20, idk='100')
        production = self.create_caf('56', 5, 15, idk='300')
        self.assertEqual((certification.environment, production.environment), ('certificacion', 'produccion'))

    def test_take_folio_sequence(self):
        Caf = self.env['tf_dte_cl.caf']
        _caf, first = Caf._tf_dte_cl_take_folio(self.company, '33')
        _caf, second = Caf._tf_dte_cl_take_folio(self.company, '33')
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.caf_33.next_folio, 3)

    def test_take_folio_only_from_company_environment(self):
        self.create_caf('34', 1, 10, idk='300')          # solo producción
        with self.assertUserError('otro ambiente'):
            self.env['tf_dte_cl.caf']._tf_dte_cl_take_folio(self.company, '34')
        self.company.tf_dte_cl_environment = 'produccion'
        _caf, folio = self.env['tf_dte_cl.caf']._tf_dte_cl_take_folio(self.company, '34')
        self.assertEqual(folio, 1)

    def test_exhausted(self):
        self.create_caf('34', 1, 1)
        Caf = self.env['tf_dte_cl.caf']
        Caf._tf_dte_cl_take_folio(self.company, '34')
        with self.assertUserError('agotado'):
            Caf._tf_dte_cl_take_folio(self.company, '34')

    def test_used_caf_cannot_be_deleted(self):
        self.env['tf_dte_cl.caf']._tf_dte_cl_take_folio(self.company, '33')
        with self.assertRaises(UserError):
            self.caf_33.unlink()

    def test_folio_cannot_go_back(self):
        self.env['tf_dte_cl.caf']._tf_dte_cl_take_folio(self.company, '33')
        with self.assertRaises(UserError):
            self.caf_33.next_folio = 1

    def test_company_rut_constant(self):
        # Garantiza que los CAF de prueba se generan para el RUT de la compañía de prueba.
        self.assertEqual(self.company.vat, COMPANY_RUT)
