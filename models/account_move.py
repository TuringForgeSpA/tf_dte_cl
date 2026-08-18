# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import io
import base64
import logging
import collections
import decimal

# NOTA SOBRE IMPORTS ELIMINADOS:
#   - facturacion_electronica: este modelo ya no habla con el SII. La
#     librería la importa (de forma defensiva) account_edi_format.py.
#   - ejemplo_basico_33: archivo de ejemplo, borrado del módulo.
#   - lxml.etree: solo lo usaba procesar_respuesta_xml().
#   - ast: solo lo usaba procesar_respuesta_boleta().

_logger = logging.getLogger(__name__)

# Estados en los que el SII ya dio una respuesta DEFINITIVA sobre el
# documento. En cualquiera de ellos la factura no puede volver a borrador
# ni eliminarse: para revertirla hay que emitir una Nota de Crédito
# referenciada (código 61).
ESTADOS_DTE_BLOQUEANTES = ('Aceptado', 'Rechazado', 'Reparo')


class DocumentosSII(models.Model):
    _name = 'account.move.docs.sii'
    _description = 'Documentos SII'

    codigo = fields.Char(string='Código', size=8)
    name = fields.Char(string='Nombre', size=64)

    def es_boleta(self):
        if self.codigo in ['35', '38', '39', '41', '70', '71']:
            return True
        return False


class Referencias(models.Model):
    _name = 'account.move.referencia'
    _description = 'Línea de referencia de Documentos DTE'

    COD_REF = [
        ('1', 'Anula Documento'),
        ('2', 'Corrige texto'),
        ('3', 'Corrige montos'),
    ]

    folio = fields.Char(string='Folio')
    fecha_documento = fields.Date(string='Fecha Documento', required=True)
    tipo_documento = fields.Many2one('account.move.docs.sii', string='Tipo documento')
    codigo_ref = fields.Selection(COD_REF, string='Código Ref.')
    motivo = fields.Char(string='Motivo')
    ref_global = fields.Boolean(string='Global')
    move_id = fields.Many2one('account.move', ondelete='cascade', index=True, copy=False, string='Documento')


class AccountMove(models.Model):
    """Extensión chilena de account.move.

    Tras la migración al framework EDI, este modelo conserva únicamente:
      - Los campos propios del DTE (folio, track_id, estado_dte, etc.).
      - Los "builders" que arma account_edi_format.py para construir el
        diccionario que consume la librería facturacion_electronica
        (data_emisor, data_documento, data_firma_electronica, receptor,
        totales, detalle_doc, referencias, caf_file, verifica_folio...).
      - Las utilidades de formato e impresión usadas por los reportes.

    TODA la transmisión al SII (timbrado, envío del sobre, captura del
    Track ID y consulta de estado) vive ahora en
    account.edi.format._l10n_cl_* y se orquesta con account.edi.document.
    """
    _inherit = 'account.move'

    def doc_por_defecto(self, company_id):
        company = self.env['res.company'].browse(company_id)
        res = company.doc_defecto or '39'
        return res

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        # TODO Odoo 17+: account.move ya no define _onchange_journal() en
        # el core, por lo que este super() levanta AttributeError. Al
        # portar, quitar el super() o renombrar el onchange.
        super(AccountMove, self)._onchange_journal()
        if self.journal_id:
            if 'Boleta' in self.journal_id.name:
                domain = [('vat', '=', '66666666-6'), ('name', '=', 'Cliente Boleta')]
                partner_id = self.env['res.partner'].search(domain, limit=1)
                if partner_id:
                    self.partner_id = partner_id
                    self.partner_shipping_id = partner_id
            else:
                self.partner_id = False
                self.partner_shipping_id = False

    @api.model
    def _get_default_journal(self):
        journal = super(AccountMove, self)._get_default_journal()
        if self._context.get('default_move_type') == 'out_refund':
            company_id = self._context.get('default_company_id', self.env.company.id)
            domain = [('company_id', '=', company_id), ('type', '=', 'sale')]
            domain += [('cod_dte', 'in', ['61'])]
            res = self.env['account.journal'].search(domain)
            if res:
                journal = res
        elif self._context.get('default_move_type') == 'out_invoice':
            company_id = self._context.get('default_company_id', self.env.company.id)
            domain = [('company_id', '=', company_id), ('type', '=', 'sale')]
            doc_defecto = self.doc_por_defecto(company_id)
            domain += [('cod_dte', '=', doc_defecto)]
            res = self.env['account.journal'].search(domain)
            if res:
                journal = res
        return journal

    @api.model
    def _get_default_sucursal(self):
        res = False
        if self.env.user.sucursal_id:
            res = self.env.user.sucursal_id.id
        return res

    # ==========================================================
    # UTILIDADES DE FORMATO
    # ==========================================================

    def formatoNumero(self, numero):
        if not numero:
            return ''
        numero = str(numero)
        numero = numero.replace('.', '')
        nn = ''
        negativo = False
        if '-' in numero:
            negativo = True
            numero = str(abs(float(numero)))
        largo_cadena = float(len(numero))
        if largo_cadena % 3 == 0:
            rango = int(largo_cadena / 3)
        else:
            rango = int(largo_cadena / 3) + 1
        for i in range(rango):
            if len(numero) >= 3:
                if nn == '':
                    nn += numero[-3:]
                else:
                    nn = numero[-3:] + '.' + nn
                numero = numero[:-3]
            else:
                if nn:
                    nn = numero + '.' + nn
                else:
                    nn = numero
        if nn:
            if negativo:
                nn = '-' + nn
            res = nn
        else:
            res = numero
        return res

    def formatoRut(self, rut):
        res = ''
        if rut:
            rut = rut.split('-')
            res = self.formatoNumero(rut[0]) + '-' + rut[1]
        return res

    def contacto(self, partner):
        res = ''
        if partner.child_ids:
            for con in partner.child_ids:
                if con.type == 'contact':
                    res += con.name + ' - '
                    res += con.email + ' - ' or ''
                    res += con.phone + ' - ' or ''
                    res += con.mobile or ''
        else:
            if partner.phone:
                res += partner.phone
            if partner.mobile:
                if res:
                    res += ' - '
                res += partner.mobile
            if partner.email:
                if res:
                    res += ' - '
                res += partner.email
        return res

    def exento(self):
        exento = 0
        for l in self.invoice_line_ids:
            if l.tax_ids.amount == 0:
                exento += l.price_subtotal
        return exento if exento > 0 else (exento * -1)

    def descuento(self, descuento):
        res = '0,0'
        if descuento:
            res = descuento
            res = str(res)
            if '.' in res:
                res = res.split('.')
                res = self.formatoNumero(res[0]) + ',' + res[1]
        return res

    def getTotalDiscount(self):
        total_discount = 0
        for l in self.invoice_line_ids:
            if not l.account_id:
                continue
            total = l.currency_id.round(l.quantity * l.price_unit)
            decimal.getcontext().rounding = decimal.ROUND_HALF_UP
            total_discount += int(decimal.Decimal(total * ((l.discount or 0.0) / 100.0)).to_integral_value())
        return self.currency_id.round(total_discount)

    def nombre_impuesto(self, texto):
        res = ''
        texto = texto.split()
        if len(texto) >= 2:
            res = texto[0] + ' ' + texto[1]
        return res

    def precio_unitario(self, linea):
        if self.es_boleta():
            impuesto = 0.0
            for tax in linea.tax_ids:
                impuesto += (tax.amount / 100 * linea.price_unit)
            decimal.getcontext().rounding = decimal.ROUND_HALF_UP
            precio = int(decimal.Decimal(impuesto + linea.price_unit).to_integral_value())
        else:
            precio = linea.price_unit
        return precio

    def _get_printed_report_name(self):
        self.ensure_one()
        report_string = "%s %s" % (self.journal_id.name, self.folio(self.name))
        return report_string

    def precio_total(self, linea):
        if self.es_boleta():
            res = linea.price_total
        else:
            res = linea.price_subtotal
        return res

    def es_boleta(self):
        try:
            cod_dte = int(self.journal_id.cod_dte)
        except:
            cod_dte = False
        if cod_dte and cod_dte in [35, 38, 39, 41, 70, 71]:
            return True
        return False

    def es_nc_boleta(self):
        if not self.referencias_ids or self.move_type != "out_refund":
            return False
        return any(r.tipo_documento.es_boleta() for r in self.referencias_ids)

    def sii_header(self):
        # TODO Pillow 10+: ImageDraw.textsize() fue eliminado; reemplazar
        # por textbbox()/textlength() al actualizar el entorno.
        font1 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
        font2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 9)
        W, H = (300, 150)
        img = Image.new("RGB", (W, H), color=(255, 255, 255))

        d = ImageDraw.Draw(img)
        d.rectangle(((0, 0), (298, 113)), outline="black", width=4)

        config_id = self.journal_id.config_dte_id
        rut = self.formatoRut(config_id.rutemisor)
        rut_txt = 'R.U.T.: ' + rut
        nom_doc_txt = self.journal_id.name
        num_doc_txt = 'N° ' + self.numero_documento()
        sii_txt = 'SII ' + config_id.oficinasii
        w, h = d.textsize(rut_txt, font=font1)
        d.text(((W - w) / 2, 20), rut_txt, fill=(0, 0, 0), font=font1)
        w, h = d.textsize(nom_doc_txt, font=font1)
        d.text(((W - w) / 2, 50), nom_doc_txt, fill=(0, 0, 0), font=font1)
        w, h = d.textsize(num_doc_txt, font=font1)
        d.text(((W - w) / 2, 80), num_doc_txt, fill=(0, 0, 0), font=font1)
        w, h = d.textsize(sii_txt, font=font2)
        d.text(((W - w) / 2, 120), sii_txt, fill=(0, 0, 0), font=font2)

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        imm = base64.b64encode(buffered.getvalue()).decode()
        return imm

    @api.depends('company_id', 'invoice_filter_type_domain')
    def _compute_suitable_journal_ids(self):
        for m in self:
            journal_type = m.invoice_filter_type_domain or 'general'
            company_id = m.company_id.id or self.env.company.id
            domain = [('company_id', '=', company_id), ('type', '=', journal_type)]
            LISTA_DTE_VENTA = ['33', '34', '39', '43', '56']
            if m.move_type == 'out_invoice':
                domain += [('cod_dte', 'in', LISTA_DTE_VENTA)]
            elif m.move_type == 'out_refund':
                domain += [('cod_dte', 'in', ['61'])]
            m.suitable_journal_ids = self.env['account.journal'].search(domain)

    # ==========================================================
    # CAMPOS
    # ==========================================================
    # TODO Odoo 17+: el atributo `states` fue eliminado de los campos.
    # Al portar, quitar `states={'draft': [('readonly', False)]}` y
    # controlar el readonly en la vista con readonly="state != 'draft'".

    indservicio = fields.Integer(string='IndServicio', default=3, copy=False)
    indmntneto = fields.Integer(string='IndMntNeto', default=2, copy=False)
    xml_envio_id = fields.Many2one('xml.envio', string='Archivo XML', readonly=True, copy=False)
    track_id = fields.Char(string='Track ID', size=64, copy=False)
    estado_dte = fields.Char(string='Estado DTE', size=128, readonly=True, copy=False)
    detalle_estado = fields.Text('Detalle estado DTE', readonly=True, copy=False)
    referencias_ids = fields.One2many('account.move.referencia', 'move_id', readonly=True, states={'draft': [('readonly', False)]}, copy=False)
    suitable_journal_ids = fields.Many2many('account.journal', compute='_compute_suitable_journal_ids', copy=False)
    sucursal_id = fields.Many2one('config.sucursales', string='Sucursal', default=_get_default_sucursal, readonly=True, states={'draft': [('readonly', False)]}, copy=False)
    journal_id = fields.Many2one('account.journal', string='Journal', required=True, readonly=True,
        states={'draft': [('readonly', False)]},
        check_company=True, domain="[('id', 'in', suitable_journal_ids)]",
        default=_get_default_journal, copy=False)

    # ==========================================================
    # IMPRESIÓN
    # ==========================================================

    def termica_a4(self, conf, cod_dte):
        fact = False
        bol = False
        fimp = conf.formato_impresion
        if fimp == 'a4ter':
            fact = True
        elif fimp == 'tera4':
            bol = True
        elif fimp == 'terter':
            bol = True
            fact = True
        if self.env.context.get('termica', False):
            if cod_dte == '39':
                bol = True
            elif cod_dte in ['33', '34', '43', '61', '56']:
                fact = True
        elif self.env.context.get('a4', False):
            if cod_dte == '39':
                bol = False
            elif cod_dte in ['33', '34', '43', '61', '56']:
                fact = False
        return fact, bol

    def imprimir_dte(self):
        self.ensure_one()
        accion = True
        if self.journal_id.cod_dte:
            conf = self.journal_id.config_dte_id
            cdte = self.journal_id.cod_dte
            factura_termica, boleta_termica = self.termica_a4(conf, cdte)
            if cdte == '39':
                if boleta_termica:
                    accion = 'boleta termica'
                else:
                    accion = 'boleta'

            elif cdte in ['33', '34', '43', '61', '56']:
                if factura_termica:
                    accion = 'factura_nota termica'
                else:
                    accion = self.env.ref('mt_dte.action_imprimir_dte').report_action(self)
        return accion

    def sucursal_ok(self, sucursal):
        res = True
        usuario = self.user_id
        if self.sucursal_id and sucursal:
            if sucursal.id != self.sucursal_id.id:
                res = False
        elif usuario.sucursal_id and sucursal:
            if sucursal.id != usuario.sucursal_id.id:
                res = False
        return res

    def barcode_imagen(self, imagen):
        """Redimensiona el timbre (TED) para la representación impresa.
        La usan las plantillas QWeb de los reportes.
        """
        ted = base64.b64decode(imagen)
        ted = io.BytesIO(ted)
        im = Image.open(ted)
        newsize = (320, 192)
        im1 = im.resize(newsize)
        buffered = io.BytesIO()
        im1.save(buffered, format="PNG")
        res = base64.b64encode(buffered.getvalue()).decode()
        return res

    def mes_texto(self, mes):
        if mes:
            if mes == '01':
                res = 'Enero'
            elif mes == '02':
                res = 'Febrero'
            elif mes == '03':
                res = 'Marzo'
            elif mes == '04':
                res = 'Abril'
            elif mes == '05':
                res = 'Mayo'
            elif mes == '06':
                res = 'Junio'
            elif mes == '07':
                res = 'Julio'
            elif mes == '08':
                res = 'Agosto'
            elif mes == '09':
                res = 'Septiembre'
            elif mes == '10':
                res = 'Octubre'
            elif mes == '11':
                res = 'Noviembre'
            elif mes == '12':
                res = 'Diciembre'
            else:
                res = ''
            return res
        else:
            res = ''
            return res

    def diadelasemana(self, fecha):
        dia_fecha = fecha.weekday()
        if dia_fecha == 0:
            res = u'Lunes'
        elif dia_fecha == 1:
            res = u'Martes'
        elif dia_fecha == 2:
            res = u'Miércoles'
        elif dia_fecha == 3:
            res = u'Jueves'
        elif dia_fecha == 4:
            res = u'Viernes'
        elif dia_fecha == 5:
            res = u'Sábado'
        elif dia_fecha == 6:
            res = u'Domingo'
        else:
            res = ''
        return res

    def fecha_documento(self, fecha):
        res = self.diadelasemana(fecha)
        fecha_texto = fecha.strftime("%d-%m-%Y")
        fecha_texto = fecha_texto.split('-')
        mes = self.mes_texto(fecha_texto[1])
        res += ', ' + fecha_texto[0] + ' de ' + mes + ' de ' + fecha_texto[2]
        return res

    def numero_documento(self):
        self.ensure_one()
        res = ''
        for letra in self.name:
            try:
                res += str(int(letra))
            except:
                res = res
        if res:
            try:
                res = self.formatoNumero(res)
            except:
                res = res
        return res

    def nombre_referencia(self, num_ref):
        res = ''
        cref = {
            '1': 'Anula Documento',
            '2': 'Corrige texto',
            '3': 'Corrige montos',
        }
        if num_ref in cref:
            res = cref[num_ref]
        return res

    def fecha_resol(self, fecha):
        fecha = fecha.split('-')
        res = fecha[0]
        return res

    def currency_format(self, val, application='Product Price'):
        code = self._context.get('lang') or self.partner_id.lang
        lang = self.env['res.lang'].search([('code', '=', code)])
        precision = self.env['decimal.precision'].precision_get(application)
        string_digits = '%.{}f'.format(precision)
        res = lang.format(string_digits, val, grouping=True, monetary=True)
        if self.currency_id.symbol:
            if self.currency_id.position == 'after':
                res = '%s %s' % (res, self.currency_id.symbol)
            elif self.currency_id.position == 'before':
                res = '%s %s' % (self.currency_id.symbol, res)
        return res

    # ==========================================================
    # BUILDERS DEL PAYLOAD DTE
    # ==========================================================
    # Estos métodos son la API que consume account_edi_format.py:
    #   data_emisor / data_firma_electronica / data_documento / api_dte
    # (y todo lo que cuelga de ellos). No modificar sus firmas sin
    # ajustar _l10n_cl_build_dte_payload y _l10n_cl_query_dte_status.

    def actecos(self, conf):
        res = list()
        for act in conf.actecos:
            res += [int(act.name)]
        if not res:
            raise UserError('Debe agregar al menos un Acteco en la configuración.')
        return res

    def data_emisor(self, conf):
        res = collections.OrderedDict()
        if not conf:
            return res
        res['RUTEmisor'] = conf.rutemisor
        res['RznSoc'] = conf.name
        res['GiroEmis'] = conf.giroemisor
        res['Actecos'] = self.actecos(conf)
        res['DirOrigen'] = conf.dirorigen
        res['CmnaOrigen'] = conf.cmnaorigen
        res['CiudadOrigen'] = conf.ciudadorigen
        res['CorreoEmisor'] = conf.correo or 'contacto@mallconnection.com'
        res['Modo'] = conf.modo
        res['NroResol'] = conf.nroresol
        res['FchResol'] = conf.fchresol
        res['ValorIva'] = int(float(conf.valoriva))
        return res

    def totales(self, inv, conf):
        res = dict()
        res['MntNeto'] = int(inv.amount_untaxed)
        res['TasaIVA'] = float(conf.valoriva)
        res['IVA'] = int(inv.amount_tax)
        res['MntTotal'] = int(inv.amount_total)
        return res

    def folio(self, nombre):
        res = ''
        for letra in nombre:
            try:
                res += str(int(letra))
            except:
                res = res
        return res

    def id_doc(self, inv, cod_dte):
        res = collections.OrderedDict()
        if cod_dte in ['39', '41']:
            res['TipoDTE'] = int(inv.journal_id.cod_dte)
        res['Folio'] = self.folio(inv.name)
        res['FchEmis'] = inv.date.strftime("%Y-%m-%d")
        if cod_dte in ['39', '41']:
            res['IndServicio'] = inv.indservicio
            res['IndMntNeto'] = inv.indmntneto
        return res

    def cod_imp(self, impuestos):
        res = 14
        for imp in impuestos:
            if imp.codigo_sii:
                res = imp.codigo_sii
        return res

    def detalle_doc(self, inv, conf, cod_dte):
        res = list()

        if cod_dte in ['39', '41']:
            iva_float = float(conf.valoriva)
            iva_float = 1.0 + (iva_float / 100.0)

        nro_lin_det = 1
        for linea in inv.invoice_line_ids:
            dicc_t = dict()
            dicc_t['NroLinDet'] = nro_lin_det
            nro_lin_det += 1
            dicc_t['CdgItem'] = dict()
            dicc_t['CdgItem']['TpoCodigo'] = 'INT1'
            dicc_t['CdgItem']['VlrCodigo'] = linea.product_id.default_code or 'SC'
            dicc_t['NmbItem'] = linea.product_id.name
            dicc_t['DscItem'] = linea.product_id.description or ''
            dicc_t['QtyItem'] = linea.quantity
            dicc_t['UnmdItem'] = 'Unid'
            if cod_dte in ['39', '41']:
                dicc_t['PrcItem'] = int(round(linea.price_unit * iva_float))
                total = linea.currency_id.round(linea.quantity * linea.price_unit * iva_float)
            else:
                dicc_t['PrcItem'] = int(round(linea.price_unit))
                total = linea.currency_id.round(linea.quantity * linea.price_unit)
            dicc_t['Impuesto'] = list()
            dicc_t['Impuesto'] += [{'CodImp': self.cod_imp(linea.tax_ids)}]

            total_discount = int(decimal.Decimal(total * ((linea.discount or 0.0) / 100.0)).to_integral_value())

            dicc_t['DescuentoMonto'] = total_discount
            dicc_t['DescuentoPct'] = linea.discount or 0.0
            dicc_t['MontoItem'] = int(total) - total_discount
            res.append(dicc_t)
        return res

    def caf_file(self, conf, cod_dte):
        caf = False
        for linea in conf.caf_files_ids:
            if linea.name == cod_dte:
                caf = linea.caf
        res = []
        if caf:
            # codificar en base64
            caf = base64.b64encode(caf.encode())
            res = [caf]
        return res

    def referencias(self, inv, cod_dte):
        res = list()
        r = 1
        for ref in inv.referencias_ids:
            ref_dicc = collections.OrderedDict()
            ref_dicc['NroLinRef'] = r
            ref_dicc['TpoDocRef'] = ref.tipo_documento.codigo
            if ref.ref_global:
                ref_dicc['IndGlobal'] = 'true'
                ref_dicc['FolioRef'] = '0'
            else:
                ref_dicc['FolioRef'] = ref.folio
            ref_dicc['FchRef'] = ref.fecha_documento.strftime("%Y-%m-%d")
            ref_dicc['CodRef'] = ref.codigo_ref
            ref_dicc['RazonRef'] = ref.motivo
            res += [ref_dicc]
            r += 1
        if not res and cod_dte in ['61']:
            raise UserError("Debe agregar al menos una referencia en las notas de crédito.")
        return res

    def revisar_cliente(self, cliente):
        res = True
        if not cliente.vat:
            res = False
        if not cliente.name:
            res = False
        if not (cliente.phone or cliente.email):
            res = False
        if not cliente.street:
            res = False
        if not cliente.state_id:
            res = False
        if not cliente.city:
            res = False
        if not cliente.comuna_id:
            res = False
        if not cliente.giro:
            res = False
        return res

    def receptor(self, inv):

        cliente = inv.partner_id

        cliente_ok = self.revisar_cliente(cliente)

        if not cliente_ok:
            raise UserError("Faltan datos del cliente. Por favor revise.")

        res = collections.OrderedDict()
        res['RUTRecep'] = cliente.vat
        res['RznSocRecep'] = cliente.name
        res['Contacto'] = cliente.phone or cliente.email
        res['GiroRecep'] = cliente.giro
        res['DirRecep'] = cliente.street
        res['CmnaRecep'] = cliente.comuna_id.name
        res['CiudadRecep'] = cliente.city

        return res

    def verifica_folio(self, conf, cod_dte, Id_doc):
        res = True
        folio = int(Id_doc['Folio'])
        desde = False
        hasta = False
        for linea in conf.caf_files_ids:
            if linea.name == cod_dte:
                desde = int(linea.desde)
                hasta = int(linea.hasta)
        if desde and hasta and folio:
            if folio < desde:
                res = False
            if folio > hasta:
                res = False
        else:
            res = False

        if not res:
            raise UserError("El folio está fuera de rango del CAF o hay problemas en la configuracion del DTE.")
        return res

    def data_documento(self, inv, conf, cod_dte):
        res = list()

        tdicc = collections.OrderedDict()
        tdicc['TipoDTE'] = int(cod_dte)
        tdicc['caf_file'] = self.caf_file(conf, cod_dte)
        tdicc['documentos'] = list()
        dicc_temp = collections.OrderedDict()
        dicc_temp['NroDTE'] = 1
        dicc_temp['Encabezado'] = collections.OrderedDict()
        dicc_temp['Encabezado']['IdDoc'] = self.id_doc(inv, cod_dte)
        dicc_temp['Encabezado']['Emisor'] = self.data_emisor(conf)
        dicc_temp['Encabezado']['Receptor'] = self.receptor(inv)
        dicc_temp['Encabezado']['Totales'] = self.totales(inv, conf)
        dicc_temp['Detalle'] = self.detalle_doc(inv, conf, cod_dte)
        dicc_temp['Referencia'] = self.referencias(inv, cod_dte)
        tdicc['documentos'].append(dicc_temp)

        self.verifica_folio(conf, cod_dte, dicc_temp['Encabezado']['IdDoc'])

        res.append(tdicc)

        return res

    def init_signature_tf(self, init_signature):
        res = False
        if init_signature == 'V':
            res = True
        return res

    def cert_file(self, cert):
        res = base64.b64encode(cert.encode())
        return res

    def data_firma_electronica(self, conf):
        res = collections.OrderedDict()
        res['priv_key'] = conf.priv_key
        res['cert'] = conf.cert
        res['rut_firmante'] = conf.rut_firmante
        res['init_signature'] = self.init_signature_tf(conf.init_signature)
        return res

    def api_dte(self, cod_dte):
        res = False
        if cod_dte == '39':
            res = True
        return res

    # ==========================================================
    # CICLO DE VIDA
    # ==========================================================
    # NOTA: se eliminó el override de _post(). Ya no hay que interceptar
    # la validación contable para llamar a crear_dte(): el framework EDI
    # encola el documento automáticamente al publicar la factura, porque
    # account.edi.format._is_required_for_invoice() / _get_move_applicability()
    # declaran que este move necesita un DTE.

    def button_draft(self):
        """Impide volver a borrador un documento con respuesta definitiva
        del SII. Para revertirlo hay que emitir una Nota de Crédito
        referenciada (código 61).
        """
        for inv in self:
            if inv.estado_dte in ESTADOS_DTE_BLOQUEANTES:
                raise UserError(
                    'No puede volver a borrador el documento %s: el SII ya respondió "%s". '
                    'Si necesita revertirlo, emita una Nota de Crédito referenciada (código 61).'
                    % (inv.name, inv.estado_dte)
                )
            if inv.xml_envio_id:
                # xml.envio.unlink() vuelve a bloquear si el sobre ya está
                # Enviado/EnProceso/Aceptado/Reparo ante el SII.
                inv.xml_envio_id.unlink()
        return super(AccountMove, self).button_draft()

    def restablecer(self):
        """Limpia el estado local del DTE (uso administrativo).

        OJO: no toca el account.edi.document asociado; si se quiere
        reintentar el envío hay que actuar sobre el documento EDI.
        """
        for inv in self:
            self_vals = dict()
            self_vals['track_id'] = False
            self_vals['estado_dte'] = False
            self_vals['detalle_estado'] = False
            inv.write(self_vals)
        return True

    @api.depends('posted_before', 'state', 'journal_id', 'date')
    def _compute_name(self):
        amvs = self.env['account.move']
        for r in self:
            if r.state == 'draft':
                amvs += r
                continue
            secuencia = r.journal_id.secuencia_id or False
            if secuencia:
                if r.name == '/':
                    numero = secuencia._next()
                    r.name = '%s' % (numero)

        super(AccountMove, amvs)._compute_name()

    def unlink(self):
        to_unlink = self.env['account.move']
        for inv in self:
            if inv.xml_envio_id or inv.estado_dte in ESTADOS_DTE_BLOQUEANTES:
                raise UserError('No puede eliminar un documento válido ante el SII (%s).' % inv.name)
            to_unlink += inv
        return super(AccountMove, to_unlink).unlink()
