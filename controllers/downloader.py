# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import content_disposition, request


class TfDteDownloader(http.Controller):

    def _responder_xml(self, filename, contenido):
        if not contenido:
            return request.not_found()
        headers = [
            ('Content-Type', 'application/xml'),
            ('Content-Disposition', content_disposition(filename)),
        ]
        return request.make_response(contenido, headers=headers)

    @http.route(['/tf_dte_cl/download/xml/<int:xml_envio_id>'], type='http', auth='user')
    def download_xml_envio(self, xml_envio_id, **kwargs):
        envio = request.env['xml.envio'].browse(xml_envio_id).exists()
        if not envio:
            return request.not_found()
        filename = ('%s.xml' % (envio.name or envio.id)).replace(' ', '_')
        contenido = envio.sii_xml_dte or envio.sii_xml_request or ''
        return self._responder_xml(filename, contenido.encode())
