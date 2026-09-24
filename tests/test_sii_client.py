# -*- coding: utf-8 -*-
"""Cliente SII: interpretación de las respuestas de facturacion_electronica.

Ruta real: tests/test_sii_client.py

Las funciones de red de la librería se reemplazan con mocks: estas pruebas no
llaman al SII. Las respuestas XML son estructuras armadas para la prueba.
"""
import codecs
import datetime
import logging
import ssl
from unittest.mock import patch

import urllib3.filepost as filepost

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.tf_dte_cl.models import sii_client as sc

UTF8 = codecs.lookup('utf-8')[3]
ISO = codecs.lookup('ISO-8859-1')[3]

UPLOAD_OK = ('<?xml version="1.0" encoding="UTF-8"?><SII:RESPUESTA xmlns:SII="http://www.sii.cl/XMLSchema">'
             '<SII:RESP_HDR><ESTADO>EPR</ESTADO><GLOSA>Envio Procesado</GLOSA></SII:RESP_HDR>'
             '<SII:RESP_BODY><INFORMADOS>1</INFORMADOS><ACEPTADOS>{a}</ACEPTADOS>'
             '<RECHAZADOS>{r}</RECHAZADOS><REPAROS>{p}</REPAROS></SII:RESP_BODY></SII:RESPUESTA>')
DOCUMENT_XML = ('<SII:RESPUESTA xmlns:SII="http://www.sii.cl/XMLSchema"><SII:RESP_HDR>'
                '<ESTADO>{}</ESTADO><GLOSA_ESTADO>x</GLOSA_ESTADO></SII:RESP_HDR></SII:RESPUESTA>')


def base(**extra):
    payload = {
        'Emisor': {'RUTEmisor': '76000000-0', 'Modo': 'certificacion', 'NroResol': 0,
                   'FchResol': datetime.date(2026, 1, 5), 'Telefono': False, 'CdgSIISucur': None},
        'firma_electronica': {'priv_key': 'K', 'cert': 'C', 'rut_firmante': '11111111-1'},
    }
    payload.update(extra)
    return payload


def doc(**values):
    return [{'TipoDTE': 33, 'documentos': [dict({'Folio': 10}, **values)]}]


@tagged('post_install', '-at_install', 'tf_dte_cl')
class TestSiiClient(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['tf_dte_cl.sii.client']

    # --- efectos globales de la librería -----------------------------------
    def test_import_side_effects_neutralized(self):
        self.assertIsNot(ssl._create_default_https_context, ssl._create_unverified_context)
        self.assertIs(filepost.writer, UTF8)

    def test_iso_multipart_scoped(self):
        with sc._iso_multipart():
            self.assertIs(filepost.writer, ISO)
        self.assertIs(filepost.writer, UTF8)
        with self.assertRaises(RuntimeError):
            with sc._iso_multipart():
                raise RuntimeError
        self.assertIs(filepost.writer, UTF8)

    def test_log_redaction(self):
        log = logging.getLogger('facturacion_electronica.clase_util')
        with self.assertLogs(log, 'WARNING') as captured:
            log.warning(b'<DTE>' + b'x' * 400 + b'</DTE>')
            log.warning('Atributo no encontrado en Emisor: Foo')
        self.assertIn('XML omitido', captured.output[0])
        self.assertIn('Atributo no encontrado', captured.output[1])

    # --- utilidades --------------------------------------------------------
    def test_clean_payload(self):
        source = base(Documento=doc(IndExe=1, Impuesto=[]))
        cleaned = sc.clean_payload(source)
        self.assertEqual(cleaned['Emisor']['NroResol'], 0)                 # los ceros se conservan
        self.assertEqual(cleaned['Emisor']['FchResol'], '2026-01-05')
        self.assertNotIn('Telefono', cleaned['Emisor'])
        self.assertNotIn('Impuesto', cleaned['Documento'][0]['documentos'][0])
        cleaned['Emisor'].pop('RUTEmisor')
        self.assertIn('RUTEmisor', source['Emisor'])                       # copia independiente

    def test_parse_rejects_entities(self):
        evil = '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a><ESTADO>&x;</ESTADO></a>'
        self.assertNotIn('root', str(sc.parse_sii_xml(evil)))

    def test_classify_send(self):
        classify = sc.classify_send_response
        self.assertEqual(classify('').outcome, sc.SEND_UNKNOWN)
        self.assertEqual(classify({}).outcome, sc.SEND_UNKNOWN)
        result = classify({'status': 'Enviado', 'sii_send_ident': ' 123 ', 'sii_xml_response': '<x/>'})
        self.assertEqual((result.outcome, result.track_id), (sc.SEND_OK, '123'))
        self.assertEqual(classify({'status': 'NoEnviado', 'xml_resp': 'Read timed out'}).outcome,
                         sc.SEND_UNKNOWN)
        result = classify({'status': 'NoEnviado', 'sii_send_ident': '',
                           'sii_xml_response': '<RECEPCIONDTE><STATUS>7</STATUS></RECEPCIONDTE>'})
        self.assertEqual((result.outcome, result.upload_status), (sc.SEND_FAILED, '7'))
        result = classify({'status': 'NoEnviado', 'sii_send_ident': '',
                           'sii_xml_response': '<RECEPCIONDTE><STATUS>5</STATUS></RECEPCIONDTE>'})
        self.assertEqual(result.outcome, sc.SEND_RETRY)

    def test_upload_interpretation(self):
        interpret = lambda **k: sc.interpret_upload_values(sc.parse_sii_xml(UPLOAD_OK.format(**k)))
        self.assertEqual(interpret(a=1, r=0, p=0), sc.UPLOAD_STATE_ACCEPTED)
        self.assertEqual(interpret(a=0, r=0, p=1), sc.UPLOAD_STATE_OBJECTIONS)
        self.assertEqual(interpret(a=0, r=1, p=0), sc.UPLOAD_STATE_REJECTED)
        self.assertEqual(interpret(a=0, r=2, p=0), sc.UPLOAD_STATE_REJECTED)
        self.assertIsNone(sc.interpret_upload_values({'ESTADO': 'SOK'}))
        self.assertEqual(sc.interpret_upload_values({'ESTADO': 'RCT'}), sc.UPLOAD_STATE_REJECTED)

    # --- llamadas a la librería (con mocks) --------------------------------
    def test_sign(self):
        responses = [
            {'TipoDTE': 33, 'Folio': 10, 'MntNeto': 100, 'MntIVA': 19, 'MntTotal': 119, 'MntExe': 0,
             'ImptoReten': 0, 'sii_xml_dte': '<DTE/>', 'sii_barcode': '<TED/>', 'sii_barcode_img': 'iVBO'},
            {'TipoDTE': 33, 'Folio': 11, 'error': 'Debe ir IVA\nTraceback (most recent call last): ...'},
        ]
        with patch.object(sc.fe, 'timbrar', return_value=responses):
            ok, bad = self.client.tf_dte_cl_sign(base(Documento=doc()))
        self.assertTrue(ok.ok)
        self.assertEqual(ok.amounts['MntTotal'], 119)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.message, 'Debe ir IVA')                        # sin el traceback
        with self.assertRaises(UserError):
            self.client.tf_dte_cl_sign(base(Documento=[{'TipoDTE': 39, 'documentos': [{'Folio': 1}]}]))
        payload = base(Documento=doc())
        payload['Emisor']['Modo'] = 'pruebas'
        with self.assertRaises(UserError):
            self.client.tf_dte_cl_sign(payload)

    def test_sign_without_barcode_fails(self):
        with patch.object(sc.fe, 'timbrar', return_value=[
                {'TipoDTE': 33, 'Folio': 10, 'sii_xml_dte': '<DTE/>', 'sii_barcode': False}]):
            self.assertFalse(self.client.tf_dte_cl_sign(base(Documento=doc()))[0].ok)

    def test_envelope(self):
        payload = base(Documento=doc(sii_xml_request='<DTE/>'), ID='TFDTE1', filename='EnvioDTE_1.xml')
        with patch.object(sc.fe, 'xml_envio', return_value={
                'status': 'draft', 'sii_xml_request': '<?xml?><EnvioDTE/>',
                'sii_send_filename': 'EnvioDTE_1.xml', 'errores': []}):
            self.assertTrue(self.client.tf_dte_cl_build_envelope(payload).ok)
        with patch.object(sc.fe, 'xml_envio', return_value={'status': 'draft', 'errores': [
                {'TipoDTE': 33, 'Folio': 10, 'error': 'boom\ntrace'}, 'No se creó xml']}):
            result = self.client.tf_dte_cl_build_envelope(payload)
        self.assertEqual((result.ok, result.message), (False, 'T33F10: boom; No se creó xml'))
        with self.assertRaises(UserError):
            self.client.tf_dte_cl_build_envelope(dict(payload, ID='1 malo'))

    def test_send_unknown_and_scoped_encoding(self):
        seen = {}

        def enviar(vals):
            seen['writer'] = filepost.writer
            vals.pop('Emisor')          # la librería muta el dict recibido
            return ''

        payload = base(sii_xml_request='<EnvioDTE/>', ID='TFDTE1', filename='EnvioDTE_1.xml')
        with patch.object(sc.fe, 'enviar_xml', side_effect=enviar):
            result = self.client.tf_dte_cl_send_envelope(payload)
        self.assertEqual(result.outcome, sc.SEND_UNKNOWN)
        self.assertIs(seen['writer'], ISO)
        self.assertIn('Emisor', payload)
        with patch.object(sc.fe, 'enviar_xml', side_effect=ConnectionError('reset')):
            self.assertEqual(self.client.tf_dte_cl_send_envelope(payload).outcome, sc.SEND_UNKNOWN)
        self.assertIs(filepost.writer, UTF8)

    def test_query_upload(self):
        payload = base(codigo_envio='123')
        with patch.object(sc.fe, 'consulta_estado_envio', return_value={
                'status': 'Enviado', 'xml_resp': '', 'errores': ['No hay Token']}):
            self.assertFalse(self.client.tf_dte_cl_query_upload(payload).ok)
        with patch.object(sc.fe, 'consulta_estado_envio', return_value={
                'status': 'Aceptado', 'xml_resp': UPLOAD_OK.format(a=0, r=0, p=1)}):
            result = self.client.tf_dte_cl_query_upload(payload)
        self.assertEqual((result.ok, result.code, result.state), (True, 'EPR', sc.UPLOAD_STATE_OBJECTIONS))
        with patch.object(sc.fe, 'consulta_estado_envio',
                          side_effect=AttributeError("'NoneType' object has no attribute 'text'")):
            self.assertFalse(self.client.tf_dte_cl_query_upload(payload).ok)

    def test_query_document(self):
        payload = base(Documento=doc(FchEmis='2026-09-01', MntTotal=119, Receptor={'RUTRecep': '1-9'}))
        with patch.object(sc.fe, 'consulta_estado_dte', return_value={
                'T33F10': {'status': 'Rechazado', 'xml_resp': DOCUMENT_XML.format('FAU')}}):
            result = self.client.tf_dte_cl_query_document(payload)
        self.assertEqual((result.ok, result.code, result.received), (True, 'FAU', False))
        with patch.object(sc.fe, 'consulta_estado_dte', return_value={
                'T33F10': {'status': 'Proceso', 'xml_resp': DOCUMENT_XML.format('DOK')}}):
            self.assertTrue(self.client.tf_dte_cl_query_document(payload).received)
        with patch.object(sc.fe, 'consulta_estado_dte', return_value={
                'T33F10': {'status': 'Enviado', 'xml_resp': '', 'errores': ['x']}}):
            self.assertFalse(self.client.tf_dte_cl_query_document(payload).ok)
