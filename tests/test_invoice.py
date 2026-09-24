# -*- coding: utf-8 -*-
"""Facturas y notas: ciclo completo con el SII simulado.

Ruta real: tests/test_invoice.py
"""
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.tf_dte_cl.models import sii_client as sc

from .common import TfDteClCommon


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestInvoice(TfDteClCommon):

    def post(self, move=None):
        move = move or self.create_invoice()
        move.action_post()
        return move

    # --- publicación: folio, firma y sobre ---------------------------------
    def test_post_signs_and_packs(self):
        with self.fake_sii() as fake:
            move = self.post()
        self.assertEqual(move.tf_dte_cl_state, 'signed')
        self.assertEqual((move.tf_dte_cl_document_type, move.tf_dte_cl_folio), ('33', 1))
        self.assertEqual(move.tf_dte_cl_caf_id, self.caf_33)
        self.assertEqual(self.caf_33.next_folio, 2)
        self.assertTrue(move.tf_dte_cl_xml_file)
        self.assertEqual(move.tf_dte_cl_envelope_id.state, 'draft')
        self.assertEqual(move.tf_dte_cl_total_amount, 23800)
        self.assertEqual(move.tf_dte_cl_title, 'Factura electrónica N° 1')
        self.assertEqual(fake.calls['send'], 0)                    # publicar no usa la red

    def test_sent_payload_lines(self):
        with self.fake_sii() as fake:
            self.post()
        document = fake.last_sign_payload['Documento'][0]['documentos'][0]
        item = document['Detalle'][0]
        self.assertEqual((item['QtyItem'], item['PrcItem'], item['MontoItem']), (2, 10000, 20000))
        self.assertEqual(item['Impuesto'], [{'CodImp': 14, 'TasaImp': 19.0}])

    def test_amount_mismatch_blocks_and_returns_folio(self):
        with self.fake_sii() as fake:
            fake.amount_delta = 1
            move = self.create_invoice()
            with self.assertUserError('no coinciden'):
                move.action_post()
        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.tf_dte_cl_folio)
        self.assertEqual(self.caf_33.next_folio, 1)                 # el folio vuelve al CAF

    def test_missing_receiver_data_blocks(self):
        self.partner.tf_dte_cl_giro = False
        with self.fake_sii() as fake:
            move = self.create_invoice()
            with self.assertUserError('giro'):
                move.action_post()
        self.assertEqual(fake.calls['sign'], 0)
        self.assertEqual(self.caf_33.next_folio, 1)

    def test_exempt_invoice_rejects_iva(self):
        journal_34 = self._create_sale_journal('TF34', '34')
        self.create_caf('34', 1, 10)
        with self.fake_sii():
            move = self.create_invoice(journal=journal_34)
            with self.assertUserError('exenta'):
                move.action_post()

    # --- envío y consulta ---------------------------------------------------
    def test_full_cycle_accepted(self):
        with self.fake_sii():
            move = self.post()
            self.assertEqual(move._tf_dte_cl_send(), 'sent')
            self.assertTrue(move.tf_dte_cl_envelope_id.track_id)
            self.assertEqual(move._tf_dte_cl_query(), 'accepted')

    def test_send_is_idempotent(self):
        with self.fake_sii() as fake:
            move = self.post()
            move._tf_dte_cl_send()
            move._tf_dte_cl_send()
        self.assertEqual(fake.calls['send'], 1)

    def test_unknown_send_then_received(self):
        with self.fake_sii() as fake:
            fake.send_outcomes = [sc.SendResult(outcome=sc.SEND_UNKNOWN, message='sin respuesta')]
            move = self.post()
            self.assertEqual(move._tf_dte_cl_send(), 'send_unknown')
            fake.document_code = 'DOK'
            self.assertEqual(move._tf_dte_cl_query(), 'accepted')
        self.assertEqual(fake.calls['send'], 1)                    # no se reenvió

    def test_unknown_send_not_received_waits(self):
        with self.fake_sii() as fake:
            fake.send_outcomes = [sc.SendResult(outcome=sc.SEND_UNKNOWN)]
            move = self.post()
            move._tf_dte_cl_send()
            fake.document_code = 'FAU'                              # el SII aún no lo tiene
            self.assertEqual(move._tf_dte_cl_query(), 'send_unknown')
        self.assertEqual(fake.calls['send'], 1)                    # se espera antes de reenviar

    def test_resend_wait_default_when_not_configured(self):
        # Sin el parámetro guardado en Ajustes debe aplicarse la espera por defecto, no 0 minutos.
        self.env['ir.config_parameter'].sudo().search([('key', '=', 'tf_dte_cl.unknown_resend_minutes')]).unlink()
        self.assertEqual(self.env['account.move']._tf_dte_cl_resend_minutes(), 60)
        self.env['ir.config_parameter'].sudo().set_param('tf_dte_cl.unknown_resend_minutes', '15')
        self.assertEqual(self.env['account.move']._tf_dte_cl_resend_minutes(), 15)

    def test_rejected_reset_takes_new_folio(self):
        with self.fake_sii() as fake:
            move = self.post()
            move._tf_dte_cl_send()
            fake.upload_state = sc.UPLOAD_STATE_REJECTED
            self.assertEqual(move._tf_dte_cl_query(), 'rejected')
            move.action_tf_dte_cl_reset()
            void = self.env['tf_dte_cl.caf.void'].search([('caf_id', '=', self.caf_33.id), ('folio', '=', 1)])
            self.assertTrue(void, 'El folio rechazado debe quedar para anulación.')
            move._tf_dte_cl_prepare()
        self.assertEqual(move.tf_dte_cl_folio, 2)                   # nunca se reutiliza el folio

    def test_accepted_cannot_go_back_to_draft(self):
        with self.fake_sii():
            move = self.post()
            move._tf_dte_cl_send()
            move._tf_dte_cl_query()
        with self.assertRaises(UserError):
            move.button_draft()

    # --- nota de crédito ----------------------------------------------------
    def test_credit_note_references_invoice(self):
        with self.fake_sii():
            invoice = self.post()
            invoice._tf_dte_cl_send()
            invoice._tf_dte_cl_query()
            refund = invoice._reverse_moves([{'ref': 'Anula factura'}], cancel=False)
        self.assertEqual(refund.journal_id, self.journal_61)
        reference = refund.tf_dte_cl_reference_ids
        self.assertEqual(len(reference), 1)
        self.assertEqual((reference.document_type_id.code, reference.folio), ('33', '1'))
        self.assertEqual(reference.code, '3')                        # corrige montos
        with self.fake_sii():
            refund.action_post()
        self.assertEqual((refund.tf_dte_cl_document_type, refund.tf_dte_cl_state), ('61', 'signed'))

    # --- impresión ----------------------------------------------------------
    def test_invoice_report_uses_dte_format(self):
        with self.fake_sii():
            move = self.post()
        html = self.env['ir.actions.report']._render_qweb_html('account.report_invoice', move.ids)[0]
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn('S.I.I. - SANTIAGO CENTRO', html)
        self.assertIn('CEDIBLE', html)                               # la factura lleva copia cedible
        self.assertIn('Timbre Electrónico SII', html)

    def test_credit_note_report_has_no_cedible_copy(self):
        with self.fake_sii():
            invoice = self.post()
            invoice._tf_dte_cl_send()
            invoice._tf_dte_cl_query()
            refund = invoice._reverse_moves([{'ref': 'Anula'}], cancel=False)
            refund.action_post()
        html = self.env['ir.actions.report']._render_qweb_html('account.report_invoice', refund.ids)[0]
        html = html.decode() if isinstance(html, bytes) else html
        self.assertNotIn('CEDIBLE', html)
