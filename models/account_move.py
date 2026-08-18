# -*- coding: utf-8 -*-
import base64
import collections
import decimal
import logging
from io import BytesIO

from lxml import etree
from PIL import Image, ImageDraw, ImageFont

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DocumentosSII(models.Model):
    _name = 'account.move.docs.sii'
    _description = 'Catálogo de tipos de documento SII'

    codigo = fields.Char(string='Código', size=8)
    name = fields.Char(string='Nombre', size=64)


class Referencias(models.Model):
    _name = 'account.move.referencia'
    _description = 'Línea de referencia de documentos DTE'

    COD_REF = [
        ('1', 'Anula documento'),
        ('2', 'Corrige texto'),
        ('3', 'Corrige montos'),
    ]

    folio = fields.Char(string='Folio')
    fecha_documento = fields.Date(string='Fecha del documento', required=True)
    tipo_documento = fields.Many2one('account.move.docs.sii', string='Tipo de documento')
    codigo_ref = fields.Selection(COD_REF, string='Código de referencia')
    motivo = fields.Char(string='Motivo')
    ref_global = fields.Boolean(string='Referencia global')
    move_id = fields.Many2one(
        'account.move', ondelete='cascade', index=True, copy=False, string='Documento contable',
    )
    pick_id = fields.Many2one(
        'stock.picking', ondelete='cascade', index=True, copy=False, string='Guía de despacho',
    )


class AccountMove(models.Model):
    _inherit = ['account.move', 'tf.dte.builder.mixin']

    LISTA_DTE_VENTA = ('33', '34', '43', '56')  # notas de crédito (61) se resuelven aparte

    # ------------------------------------------------------------------
    # Campos DTE
    # ------------------------------------------------------------------
    xml_envio_id = fields.Many2one('xml.envio', string='Sobre de envío SII', readonly=True, copy=False)
    confirmado_previamente = fields.Boolean(string='Folio ya asignado', copy=False)
    track_id = fields.Char(string='Track ID', size=64, copy=False)
    estado_dte = fields.Char(string='Estado DTE', size=128, readonly=True, copy=False)
    detalle_estado = fields.Text(string='Detalle del estado DTE', readonly=True, copy=False)
    referencias_ids = fields.One2many(
        'account.move.referencia', 'move_id', string='Referencias', copy=False,
    )
    suitable_journal_ids = fields.Many2many(
        'account.journal', compute='_compute_suitable_journal_ids', copy=False,
    )
    sucursal_id = fields.Many2one(
        'config.sucursales', string='Sucursal', default=lambda self: self._get_default_sucursal(), copy=False,
    )
    journal_id = fields.Many2one(
        'account.journal', string='Diario', required=True, readonly=True,
        check_company=True, domain="[('id', 'in', suitable_journal_ids)]",
        default=lambda self: self._get_default_journal(), copy=False,
    )

    @api.model
    def _get_default_sucursal(self):
        return self.env.user.sucursal_id.id or False

    @api.model
    def _get_default_journal(self):
        journal = super()._get_default_journal()
        move_type = self._context.get('default_move_type')
        company_id = self._context.get('default_company_id', self.env.company.id)
        if move_type == 'out_refund':
            cod_dte = '61'
        elif move_type == 'out_invoice':
            cod_dte = self.env['res.company'].browse(company_id).doc_defecto or '33'
        else:
            cod_dte = False
        if cod_dte:
            candidato = self.env['account.journal'].search([
                ('company_id', '=', company_id), ('type', '=', 'sale'), ('cod_dte', '=', cod_dte),
            ], limit=1)
            journal = candidato or journal
        return journal

    @api.depends('company_id', 'move_type')
    def _compute_suitable_journal_ids(self):
        for move in self:
            company_id = move.company_id.id or self.env.company.id
            domain = [('company_id', '=', company_id), ('type', '=', 'sale')]
            if move.move_type == 'out_invoice':
                domain += [('cod_dte', 'in', self.LISTA_DTE_VENTA)]
            elif move.move_type == 'out_refund':
                domain += [('cod_dte', '=', '61')]
            move.suitable_journal_ids = self.env['account.journal'].search(domain)

    # ------------------------------------------------------------------
    # Asignación de folio (se invoca desde account.edi.format antes de timbrar)
    # ------------------------------------------------------------------
    def _dte_asignar_folio(self):
        for move in self:
            if move.confirmado_previamente:
                continue
            secuencia = move.journal_id.secuencia_id
            if not secuencia:
                continue
            move.write({
                'name': str(secuencia.next_by_id()),
                'confirmado_previamente': True,
            })

    # ------------------------------------------------------------------
    # Builders: ensamblan el diccionario que consume la librería SII
    # ------------------------------------------------------------------
    def _dte_totales(self, conf):
        self.ensure_one()
        return {
            'MntNeto': int(self.amount_untaxed),
            'TasaIVA': float(conf.valoriva),
            'IVA': int(self.amount_tax),
            'MntTotal': int(self.amount_total),
        }

    def _dte_id_doc(self):
        self.ensure_one()
        return collections.OrderedDict(
            Folio=self._dte_folio(self.name),
            FchEmis=self.date.strftime('%Y-%m-%d'),
        )

    def _dte_cod_imp(self, impuestos):
        return next((imp.codigo_sii for imp in impuestos if imp.codigo_sii), 14)

    def _dte_detalle_doc(self):
        self.ensure_one()
        detalle = []
        lineas = self.invoice_line_ids.filtered(lambda l: not l.display_type)
        for i, linea in enumerate(lineas, start=1):
            total = linea.currency_id.round(linea.quantity * linea.price_unit)
            descuento_monto = int(
                decimal.Decimal(total * ((linea.discount or 0.0) / 100.0)).to_integral_value()
            )
            detalle.append({
                'NroLinDet': i,
                'CdgItem': {'TpoCodigo': 'INT1', 'VlrCodigo': linea.product_id.default_code or 'SC'},
                'NmbItem': linea.product_id.name,
                'DscItem': linea.product_id.description or '',
                'QtyItem': linea.quantity,
                'UnmdItem': 'Unid',
                'PrcItem': int(round(linea.price_unit)),
                'Impuesto': [{'CodImp': self._dte_cod_imp(linea.tax_ids)}],
                'DescuentoMonto': descuento_monto,
                'DescuentoPct': linea.discount or 0.0,
                'MontoItem': int(total) - descuento_monto,
            })
        return detalle

    def _dte_data_documento(self, conf, cod_dte):
        self.ensure_one()
        id_doc = self._dte_id_doc()
        encabezado = collections.OrderedDict(
            IdDoc=id_doc,
            Emisor=self._dte_data_emisor(conf),
            Receptor=self._dte_receptor(self.partner_id),
            Totales=self._dte_totales(conf),
        )
        documento = collections.OrderedDict(
            NroDTE=1,
            Encabezado=encabezado,
            Detalle=self._dte_detalle_doc(),
            # TODO: implementar descuentos/recargos globales (DscRcgGlobal) cuando se
            # requiera soportarlos; el módulo anterior nunca lo implementó (siempre []).
            Referencia=self._dte_referencias(self.referencias_ids, cod_dte),
        )
        self._dte_verifica_folio(conf, cod_dte, id_doc['Folio'])
        return [collections.OrderedDict(
            TipoDTE=int(cod_dte),
            caf_file=self._dte_caf_file(conf, cod_dte),
            documentos=[documento],
        )]

    def _dte_build_envio(self, conf, cod_dte):
        self.ensure_one()
        return collections.OrderedDict(
            Emisor=self._dte_data_emisor(conf),
            RutReceptor='60803000-K',  # RUT del SII: requerido por la librería para el canal de envío
            firma_electronica=self._dte_data_firma_electronica(conf),
            Documento=self._dte_data_documento(conf, cod_dte),
            api=False,
        )

    def _dte_build_consulta(self):
        self.ensure_one()
        conf = self.journal_id.config_dte_id
        cod_dte = self.journal_id.cod_dte
        return collections.OrderedDict(
            Emisor=self._dte_data_emisor(conf),
            firma_electronica=self._dte_data_firma_electronica(conf),
            codigo_envio=self.track_id,
            Documento=self._dte_data_documento(conf, cod_dte),
        )

    # ------------------------------------------------------------------
    # Procesamiento de respuestas del SII (llamado por account.edi.format)
    # ------------------------------------------------------------------
    def _dte_procesar_respuesta_envio(self, respuesta, cod_dte):
        self.ensure_one()
        if not isinstance(respuesta, dict):
            _logger.warning('Respuesta inesperada del SII para %s: %s', self.name, respuesta)
            return
        if self.xml_envio_id:
            self.xml_envio_id.unlink()
        xml_envio = self.env['xml.envio'].create({
            'name': 'T%sF%s' % (cod_dte, self._dte_folio(self.name)),
            'sii_send_ident': respuesta.get('sii_send_ident'),
            'sii_xml_request': respuesta.get('sii_xml_request'),
            'sii_xml_dte': respuesta.get('sii_xml_request'),
            'sii_barcode': respuesta.get('sii_barcode'),
            'move_id': self.id,
        })
        self.write({
            'xml_envio_id': xml_envio.id,
            'track_id': respuesta.get('sii_send_ident'),
            'estado_dte': respuesta.get('status'),
        })

    def _dte_interpretar_glosa(self, respuesta):
        if 'xml_resp' not in respuesta:
            return respuesta.get('glosa', '')
        resp_xml = respuesta['xml_resp'].replace('<?xml version="1.0" encoding="UTF-8"?>', '')
        root = etree.fromstring(resp_xml.encode() if isinstance(resp_xml, str) else resp_xml)
        estado, glosa, resumen = '', '', ''
        for e in root.iter():
            if e.tag == 'ESTADO':
                estado = e.text
            elif e.tag == 'GLOSA':
                glosa += e.text or ''
            elif e.tag == 'GLOSA_ERR':
                glosa += ' - %s' % (e.text or '')
            elif e.tag == 'ACEPTADOS' and e.text != '0':
                resumen = ' Aceptado'
            elif e.tag == 'RECHAZADOS' and e.text != '0':
                resumen = ' Rechazado'
            elif e.tag == 'REPAROS' and e.text != '0':
                resumen = ' Con reparo'
        return ' - '.join(filter(None, [estado, glosa, resumen]))

    def _dte_interpretar_estado(self, respuesta):
        estado = respuesta.get('status', self.estado_dte)
        if estado != 'Proceso' or 'xml_resp' not in respuesta:
            return estado
        resp_xml = respuesta['xml_resp'].replace('<?xml version="1.0" encoding="UTF-8"?>', '')
        root = etree.fromstring(resp_xml.encode() if isinstance(resp_xml, str) else resp_xml)
        dok = any(e.tag == 'ESTADO' and e.text == 'DOK' for e in root.iter())
        glosa_ok = any(
            e.tag == 'GLOSA_ERR' and e.text == 'Documento Recibido por el SII. Datos Coinciden con los Registrados'
            for e in root.iter()
        )
        if dok and glosa_ok and respuesta.get('glosa') == 'DTE Recibido':
            return 'Aceptado'
        return estado

    def _dte_procesar_respuesta_consulta(self, respuesta):
        self.ensure_one()
        if not isinstance(respuesta, dict):
            return
        clave = 'T%sF%s' % (self.journal_id.cod_dte, self._dte_folio(self.name))
        if clave in respuesta:
            respuesta = respuesta[clave]
        estado = self._dte_interpretar_estado(respuesta)
        detalle = self._dte_interpretar_glosa(respuesta)
        self.write({'estado_dte': estado, 'detalle_estado': detalle})
        if self.xml_envio_id:
            estados_validos = dict(self.xml_envio_id._fields['state'].selection)
            self.xml_envio_id.write({
                'state': estado if estado in estados_validos else self.xml_envio_id.state,
                'sii_xml_response': respuesta.get('xml_resp', self.xml_envio_id.sii_xml_response),
                'sii_receipt': detalle,
            })

    # ------------------------------------------------------------------
    # Acciones expuestas al usuario / al cron
    # ------------------------------------------------------------------
    def consulta_estado_dte(self):
        dte_format = self.env['account.edi.format'].search([('code', '=', 'tf_cl_dte')], limit=1)
        if dte_format:
            dte_format._tf_dte_cl_consultar_estado(self.filtered('track_id'))
        return True

    @api.model
    def _cron_consultar_estado_dte(self, batch_size=80):
        dte_format = self.env['account.edi.format'].search([('code', '=', 'tf_cl_dte')], limit=1)
        if not dte_format:
            return
        pendientes = self.search([
            ('journal_id.cod_dte', '!=', False),
            ('track_id', '!=', False),
            ('estado_dte', 'not in', ['Aceptado', 'Rechazado']),
        ], limit=batch_size)
        if pendientes:
            dte_format._tf_dte_cl_consultar_estado(pendientes)

    def restablecer(self):
        for move in self:
            if move.estado_dte == 'Aceptado':
                raise UserError(_('No se puede restablecer un documento ya aceptado por el SII.'))
            move.edi_document_ids.filtered(
                lambda d: d.edi_format_id.code == 'tf_cl_dte'
            ).write({'state': 'to_send', 'error': False, 'blocking_level': False})
            if move.xml_envio_id:
                move.xml_envio_id.unlink()
            move.write({'track_id': False, 'estado_dte': False, 'detalle_estado': False})
        return True

    def button_draft(self):
        bloqueados = self.filtered(lambda m: m.estado_dte == 'Aceptado')
        if bloqueados:
            raise UserError(_(
                'No puede volver a borrador un documento ya aceptado por el SII: %s.'
            ) % ', '.join(bloqueados.mapped('name')))
        return super().button_draft()

    def unlink(self):
        bloqueados = self.filtered(
            lambda m: m.xml_envio_id or m.estado_dte in ('Aceptado', 'Reparo', 'Rechazado')
        )
        if bloqueados:
            raise UserError(_(
                'No puede eliminar un documento con trámite ante el SII: %s.'
            ) % ', '.join(bloqueados.mapped('name')))
        return super().unlink()

    # ------------------------------------------------------------------
    # Impresión
    # ------------------------------------------------------------------
    def sucursal_ok(self, sucursal):
        usuario = self.user_id
        if self.sucursal_id and sucursal:
            return sucursal.id == self.sucursal_id.id
        if usuario.sucursal_id and sucursal:
            return sucursal.id == usuario.sucursal_id.id
        return True

    def termica_a4(self, conf):
        return conf.formato_impresion == 'a4ter' if not self.env.context.get('a4') else False

    def imprimir_dte(self):
        self.ensure_one()
        if not self.journal_id.cod_dte:
            return True
        reporte = 'action_imprimir_documento_termico' if self.termica_a4(self.journal_id.config_dte_id) \
            else 'action_imprimir_dte'
        return self.env.ref('tf_dte_cl.%s' % reporte).report_action(self)

    def numero_documento(self):
        self.ensure_one()
        digitos = self._dte_folio(self.name)
        return self._dte_formato_numero(digitos) if digitos else ''

    def _get_printed_report_name(self):
        self.ensure_one()
        return '%s %s' % (self.journal_id.name, self.numero_documento())

    def nombre_referencia(self, num_ref):
        return dict(Referencias.COD_REF).get(num_ref, '')

    def contacto(self, partner):
        if partner.child_ids:
            contactos = partner.child_ids.filtered(lambda c: c.type == 'contact')
            return ' - '.join(filter(None, (
                '%s %s %s %s' % (c.name, c.email or '', c.phone or '', c.mobile or '')
                for c in contactos
            )))
        return ' - '.join(filter(None, [partner.phone, partner.mobile, partner.email]))

    def exento(self):
        exento = sum(
            l.price_subtotal for l in self.invoice_line_ids if not l.tax_ids.amount
        )
        return abs(exento)

    def descuento(self, descuento):
        if not descuento:
            return '0,0'
        entero, _sep, decimales = str(descuento).partition('.')
        return '%s,%s' % (self._dte_formato_numero(entero), decimales) if decimales else str(descuento)

    def getTotalDiscount(self):
        total_discount = 0
        for linea in self.invoice_line_ids.filtered('account_id'):
            total = linea.currency_id.round(linea.quantity * linea.price_unit)
            total_discount += int(
                decimal.Decimal(total * ((linea.discount or 0.0) / 100.0)).to_integral_value()
            )
        return self.currency_id.round(total_discount)

    def nombre_impuesto(self, texto):
        palabras = (texto or '').split()
        return ' '.join(palabras[:2]) if len(palabras) >= 2 else ''

    def currency_format(self, val, application='Product Price'):
        lang = self.env['res.lang'].search([('code', '=', self._context.get('lang') or self.partner_id.lang)])
        precision = self.env['decimal.precision'].precision_get(application)
        res = lang.format('%.{}f'.format(precision), val, grouping=True, monetary=True)
        if self.currency_id.symbol:
            if self.currency_id.position == 'after':
                res = '%s %s' % (res, self.currency_id.symbol)
            else:
                res = '%s %s' % (self.currency_id.symbol, res)
        return res

    def sii_header(self):
        """Genera la caja de timbre SII (RUT / documento / folio / oficina) como PNG.
        Corregido para Pillow >= 10: `ImageDraw.textsize` fue reemplazado por `textbbox`.
        """
        self.ensure_one()
        font1 = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 15)
        font2 = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 9)
        ancho, alto = 300, 150
        img = Image.new('RGB', (ancho, alto), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle(((0, 0), (298, 113)), outline='black', width=4)

        conf = self.journal_id.config_dte_id
        rut_txt = 'R.U.T.: %s' % self._dte_formato_rut(conf.rutemisor)
        lineas = (
            (rut_txt, font1, 20),
            (self.journal_id.name, font1, 50),
            ('N° %s' % self.numero_documento(), font1, 80),
            ('SII %s' % conf.oficinasii, font2, 120),
        )
        for texto, font, y in lineas:
            _, _, ancho_texto, _ = draw.textbbox((0, 0), texto, font=font)
            draw.text(((ancho - ancho_texto) / 2, y), texto, fill=(0, 0, 0), font=font)

        buffer = BytesIO()
        img.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode()
