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

LISTA_DTE_GUIA = [('52', 'Guía de despacho electrónica (52)')]

LISTA_DESPACHO = [
    ('1', 'Por cuenta del receptor del documento (cliente)'),
    ('2', 'Por cuenta del emisor a instalaciones del cliente'),
    ('3', 'Por cuenta del emisor a otras instalaciones'),
]

LISTA_TRASLADO = [
    ('1', 'Operación constituye venta'),
    ('2', 'Ventas por efectuar'),
    ('3', 'Consignaciones'),
    ('4', 'Entrega gratuita'),
    ('5', 'Traslados internos'),
    ('6', 'Otros traslados no venta'),
    ('7', 'Guía de devolución'),
    ('8', 'Traslado para exportación (no venta)'),
    ('9', 'Venta para exportación'),
]

LISTA_PAGO = [
    ('1', 'Contado'),
    ('2', 'Crédito'),
    ('3', 'Sin costo (entrega gratuita)'),
]

# Estados de control interno del envío asíncrono, análogos a los que usa
# account_edi (to_send -> sent) pero livianos, ya que account.edi.format
# no aplica a stock.picking.
ESTADOS_ENVIO_DTE = [
    ('to_send', 'Por enviar'),
    ('sent', 'Enviado'),
    ('error', 'Con error'),
]

ESTADOS_TERMINALES_SII = ('Aceptado', 'Reparo', 'Rechazado')


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    config_dte_id = fields.Many2one('config.dte', string='Configuración DTE')
    cod_dte = fields.Selection(LISTA_DTE_GUIA, string='Código de documento SII')
    secuencia_id = fields.Many2one('ir.sequence', string='Secuencia de folios SII')
    sucursal_nombre = fields.Char(string='Nombre de la sucursal', size=64, copy=False)
    sucursal_codsii = fields.Char(string='Código de sucursal SII', size=64, copy=False)


class StockPicking(models.Model):
    _inherit = ['stock.picking', 'tf.dte.builder.mixin']

    @api.model
    def _get_default_sucursal(self):
        return self.env.user.sucursal_id.id or False

    @api.model
    def _get_default_country(self):
        return self.env['res.country'].search([('code', '=', 'CL')], limit=1).id

    # ------------------------------------------------------------------
    # Campos DTE
    # ------------------------------------------------------------------
    es_dte = fields.Boolean(string='Es DTE', compute='_compute_es_dte', store=True)
    xml_envio_id = fields.Many2one('xml.envio', string='Sobre de envío SII', readonly=True, copy=False)
    confirmado_previamente = fields.Boolean(string='Folio ya asignado', copy=False)
    track_id = fields.Char(string='Track ID', size=64, copy=False)
    dte_send_state = fields.Selection(
        ESTADOS_ENVIO_DTE, string='Estado de envío', copy=False,
        help='Control interno del proceso asíncrono de timbrado y transmisión al SII.',
    )
    estado_dte = fields.Char(string='Estado DTE', size=128, readonly=True, copy=False)
    detalle_estado = fields.Text(string='Detalle del estado DTE', readonly=True, copy=False)
    referencias_ids = fields.One2many(
        'account.move.referencia', 'pick_id', string='Referencias', copy=False,
    )
    sucursal_id = fields.Many2one(
        'config.sucursales', string='Sucursal', default=lambda self: self._get_default_sucursal(), copy=False,
    )
    tipo_despacho = fields.Selection(LISTA_DESPACHO, string='Tipo de despacho')
    indicacion_traslado = fields.Selection(LISTA_TRASLADO, string='Indicador de traslado de bienes')
    forma_pago = fields.Selection(LISTA_PAGO, string='Forma de pago')
    country_id = fields.Many2one(
        'res.country', string='País', ondelete='restrict', default=lambda self: self._get_default_country(),
    )
    state_id = fields.Many2one(
        'res.country.state', string='Región', ondelete='restrict', domain="[('country_id', '=?', country_id)]",
    )
    ciudad = fields.Char(string='Ciudad')
    comuna_id = fields.Many2one('res.comuna', string='Comuna', domain="[('state_id', '=', state_id)]")
    direccion_destino = fields.Char(string='Dirección de destino', size=128, copy=False)
    transportista_id = fields.Many2one(
        'res.partner', string='Transportista', check_company=True, domain="[('transportista', '=', True)]",
    )

    @api.depends('picking_type_id.cod_dte', 'picking_type_id.config_dte_id')
    def _compute_es_dte(self):
        for pick in self:
            pick.es_dte = bool(pick.picking_type_id.cod_dte and pick.picking_type_id.config_dte_id)

    # ------------------------------------------------------------------
    # Flujo de negocio
    # ------------------------------------------------------------------
    def action_confirm(self):
        res = super().action_confirm()
        for pick in self.filtered('es_dte'):
            if not pick.direccion_destino:
                pick._copiar_direccion_cliente()
        return res

    def _copiar_direccion_cliente(self):
        self.ensure_one()
        partner = self.partner_id
        direccion = partner.street
        if partner.street2:
            direccion = '%s, %s' % (direccion, partner.street2)
        self.write({
            'state_id': partner.state_id.id,
            'ciudad': partner.city,
            'comuna_id': partner.comuna_id.id,
            'direccion_destino': direccion,
        })

    def button_validate(self):
        """Valida la configuración DTE de forma síncrona (rápida, sin red) y deja el
        documento encolado para el cron de transmisión. La llamada real al SII
        (timbrado y envío) nunca ocurre aquí, para no bloquear la interfaz.
        """
        dte_pickings = self.filtered('es_dte')
        if dte_pickings:
            dte_pickings._dte_check_configuration()
        res = super().button_validate()
        dte_pickings.write({'dte_send_state': 'to_send'})
        return res

    def _dte_check_configuration(self):
        for pick in self:
            conf = pick.picking_type_id.config_dte_id
            cod_dte = pick.picking_type_id.cod_dte
            if not any(caf.name == cod_dte for caf in conf.caf_files_ids):
                raise UserError(_(
                    'No hay un CAF cargado en "%s" para el tipo de documento %s.'
                ) % (conf.name, cod_dte))
            faltantes = pick._dte_revisar_cliente(pick.partner_id)
            if faltantes:
                raise UserError(_(
                    'Faltan datos del cliente "%s" para emitir la guía: %s.'
                ) % (pick.partner_id.name, ', '.join(faltantes)))
            if not (pick.tipo_despacho and pick.indicacion_traslado and pick.forma_pago):
                raise UserError(_(
                    'Debe completar el tipo de despacho, el indicador de traslado y la '
                    'forma de pago antes de validar la guía.'
                ))

    def unlink(self):
        bloqueados = self.filtered(
            lambda p: p.xml_envio_id or p.estado_dte in ESTADOS_TERMINALES_SII
        )
        if bloqueados:
            raise UserError(_(
                'No puede eliminar una guía con trámite ante el SII: %s.'
            ) % ', '.join(bloqueados.mapped('name')))
        return super().unlink()

    def restablecer(self):
        for pick in self:
            if pick.estado_dte == 'Aceptado':
                raise UserError(_('No se puede restablecer una guía ya aceptada por el SII.'))
            if pick.xml_envio_id:
                pick.xml_envio_id.unlink()
            pick.write({
                'track_id': False,
                'estado_dte': False,
                'detalle_estado': False,
                'dte_send_state': 'to_send' if pick.state == 'done' else False,
            })
        return True

    # ------------------------------------------------------------------
    # Builders — reutilizan tf.dte.builder.mixin, extendidos con datos
    # propios de la guía (sucursal, tipo de despacho, forma de pago).
    # ------------------------------------------------------------------
    def _dte_data_emisor(self, conf):
        self.ensure_one()
        emisor = super()._dte_data_emisor(conf)
        emisor['Sucursal'] = self.picking_type_id.sucursal_nombre
        emisor['CdgSIISucur'] = int(self.picking_type_id.sucursal_codsii or 0) or False
        return emisor

    def _dte_tipo_impresion(self, conf):
        return 'T' if conf.formato_impresion in ('a4ter', 'terter') else 'N'

    def _dte_id_doc(self, cod_dte, conf):
        self.ensure_one()
        return collections.OrderedDict(
            Folio=self._dte_folio(self.name),
            FchEmis=self.scheduled_date.strftime('%Y-%m-%d'),
            TipoDespacho=int(self.tipo_despacho),
            IndTraslado=int(self.indicacion_traslado),
            TpoImpresion=self._dte_tipo_impresion(conf),
            FmaPago=int(self.forma_pago),
        )

    def _dte_cod_imp(self, move):
        impuestos = move.sale_line_id.tax_id if move.sale_line_id else self.env['account.tax']
        return [{'CodImp': imp.codigo_sii} for imp in impuestos if imp.codigo_sii]

    def _dte_precio_item(self, move):
        if move.sale_line_id and move.sale_line_id.price_unit:
            return move.sale_line_id.price_unit
        return 1.0

    def _dte_detalle_doc(self):
        self.ensure_one()
        detalle = []
        movimientos = self.move_ids.filtered('picked')
        for i, move in enumerate(movimientos, start=1):
            detalle.append({
                'NroLinDet': i,
                'NmbItem': move.product_id.name,
                'QtyItem': move.quantity,
                'UnmdItem': 'Unid',
                'PrcItem': self._dte_precio_item(move),
                'Impuesto': self._dte_cod_imp(move),
            })
        return detalle

    def _dte_data_documento(self, conf, cod_dte):
        self.ensure_one()
        id_doc = self._dte_id_doc(cod_dte, conf)
        encabezado = collections.OrderedDict(
            IdDoc=id_doc,
            Emisor=self._dte_data_emisor(conf),
            Receptor=self._dte_receptor(self.partner_id),
        )
        documento = collections.OrderedDict(
            NroDTE=1,
            Encabezado=encabezado,
            Detalle=self._dte_detalle_doc(),
            Referencia=self._dte_referencias(self.referencias_ids, cod_dte, requiere_referencia=()),
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
            RutReceptor='60803000-K',
            firma_electronica=self._dte_data_firma_electronica(conf),
            Documento=self._dte_data_documento(conf, cod_dte),
            api=False,
        )

    def _dte_build_consulta(self):
        self.ensure_one()
        conf = self.picking_type_id.config_dte_id
        cod_dte = self.picking_type_id.cod_dte
        return collections.OrderedDict(
            Emisor=self._dte_data_emisor(conf),
            firma_electronica=self._dte_data_firma_electronica(conf),
            codigo_envio=self.track_id,
            Documento=self._dte_data_documento(conf, cod_dte),
        )

    # ------------------------------------------------------------------
    # Asignación de folio + procesamiento de respuestas
    # ------------------------------------------------------------------
    def _dte_asignar_folio(self):
        for pick in self:
            if pick.confirmado_previamente:
                continue
            secuencia = pick.picking_type_id.secuencia_id
            if not secuencia:
                continue
            pick.write({
                'name': str(secuencia.next_by_id()),
                'confirmado_previamente': True,
            })

    def _dte_procesar_respuesta_envio(self, respuesta, cod_dte):
        self.ensure_one()
        if self.xml_envio_id:
            self.xml_envio_id.unlink()
        xml_envio = self.env['xml.envio'].create({
            'name': 'T%sF%s' % (cod_dte, self._dte_folio(self.name)),
            'sii_send_ident': respuesta.get('sii_send_ident'),
            'sii_xml_request': respuesta.get('sii_xml_request'),
            'sii_xml_dte': respuesta.get('sii_xml_request'),
            'sii_barcode': respuesta.get('sii_barcode'),
            'pick_id': self.id,
        })
        self.write({
            'xml_envio_id': xml_envio.id,
            'track_id': respuesta.get('sii_send_ident'),
            'estado_dte': respuesta.get('status'),
        })

    def _dte_procesar_respuesta_consulta(self, respuesta):
        self.ensure_one()
        if not isinstance(respuesta, dict):
            return
        clave = 'T%sF%s' % (self.picking_type_id.cod_dte, self._dte_folio(self.name))
        if clave in respuesta:
            respuesta = respuesta[clave]
        estado = respuesta.get('status', self.estado_dte)
        self.write({'estado_dte': estado})
        if self.xml_envio_id and estado in dict(self.xml_envio_id._fields['state'].selection):
            self.xml_envio_id.write({
                'state': estado,
                'sii_xml_response': respuesta.get('xml_resp', self.xml_envio_id.sii_xml_response),
            })

    # ------------------------------------------------------------------
    # Cron: envío asíncrono y consulta de estado. La llamada de red vive
    # aquí — nunca en button_validate — y cada guía se procesa de forma
    # defensiva para que un error de timeout/HTTP en una no interrumpa
    # el resto del lote ni la transacción de base de datos.
    # ------------------------------------------------------------------
    @api.model
    def _cron_procesar_guias_dte(self, batch_size=50):
        if fe is None:
            _logger.warning('No se puede procesar guías DTE: falta la librería "facturacion_electronica".')
            return
        pendientes = self.search([('dte_send_state', '=', 'to_send')], limit=batch_size)
        pendientes._dte_asignar_folio()
        for pick in pendientes:
            try:
                conf = pick.picking_type_id.config_dte_id
                cod_dte = pick.picking_type_id.cod_dte
                data = pick._dte_build_envio(conf, cod_dte)
                if conf.modo == 'pruebas':
                    _logger.info('Guía DTE en modo de pruebas, no se transmite: %s', pick.name)
                    pick.dte_send_state = 'sent'
                    continue
                respuesta = fe.timbrar_y_enviar(data)
                pick._dte_procesar_respuesta_envio(respuesta, cod_dte)
                pick.dte_send_state = 'sent'
            except UserError as error:
                pick.write({'dte_send_state': 'error', 'detalle_estado': str(error)})
            except Exception as error:  # noqa: BLE001 - error de red/timeout de la librería externa
                _logger.exception('Error al timbrar/enviar la guía %s', pick.name)
                pick.write({'dte_send_state': 'error', 'detalle_estado': str(error)})

    @api.model
    def _cron_consultar_estado_dte(self, batch_size=80):
        if fe is None:
            return
        pendientes = self.search([
            ('es_dte', '=', True),
            ('track_id', '!=', False),
            ('estado_dte', 'not in', list(ESTADOS_TERMINALES_SII)),
        ], limit=batch_size)
        for pick in pendientes:
            try:
                respuesta = fe.consulta_estado_dte(pick._dte_build_consulta())
                pick._dte_procesar_respuesta_consulta(respuesta)
            except Exception:  # noqa: BLE001
                _logger.exception('Error al consultar el estado SII de la guía %s', pick.name)

    def consulta_estado_dte(self):
        self._cron_consultar_estado_dte.__func__(self.browse(self.ids))
        return True

    # ------------------------------------------------------------------
    # Impresión
    # ------------------------------------------------------------------
    def imprimir_dte(self):
        self.ensure_one()
        conf = self.picking_type_id.config_dte_id
        if not conf:
            return True
        reporte = 'action_imprimir_documento_termico' if conf.formato_impresion in ('tera4', 'terter') \
            else 'action_imprimir_dte_guia'
        return self.env.ref('tf_dte_cl.%s' % reporte).report_action(self)

    def _get_printed_report_name(self):
        self.ensure_one()
        return '%s %s' % (self.picking_type_id.name, self._dte_formato_numero(self._dte_folio(self.name)))
