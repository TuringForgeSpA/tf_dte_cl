# -*- coding: utf-8 -*-
import base64
import collections
import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TfDteBuilderMixin(models.AbstractModel):
    """Utilidades comunes para construir el diccionario de datos que consume
    la librería `facturacion_electronica`. Se comparte entre `account.move`
    (facturas, notas de crédito/débito) y `stock.picking` (guías de despacho),
    evitando la duplicación de builders que tenía el módulo anterior.
    """
    _name = 'tf.dte.builder.mixin'
    _description = 'Utilidades comunes para armar datos de DTE'

    # ------------------------------------------------------------------
    # Formato de números / RUT / folio
    # ------------------------------------------------------------------
    def _dte_formato_numero(self, numero):
        if not numero:
            return ''
        numero = str(numero).replace('.', '')
        negativo = numero.startswith('-')
        if negativo:
            numero = str(abs(float(numero)))
        nn = ''
        while len(numero) > 3:
            nn = numero[-3:] + ('.' + nn if nn else '')
            numero = numero[:-3]
        nn = numero + ('.' + nn if nn else '') if nn else numero
        return ('-' + nn) if negativo else nn

    def _dte_formato_rut(self, rut):
        if not rut or '-' not in rut:
            return rut or ''
        cuerpo, dv = rut.rsplit('-', 1)
        return '%s-%s' % (self._dte_formato_numero(cuerpo), dv)

    def _dte_folio(self, nombre):
        """Extrae solo los dígitos del nombre de la secuencia (ej: 'FAC/2026/00045' -> '202600045')."""
        return ''.join(c for c in (nombre or '') if c.isdigit())

    # ------------------------------------------------------------------
    # Emisor / firma / CAF
    # ------------------------------------------------------------------
    def _dte_actecos(self, conf):
        actecos = [int(a.name) for a in conf.actecos]
        if not actecos:
            raise UserError(_('Debe agregar al menos una actividad económica (Acteco) en la configuración DTE.'))
        return actecos

    def _dte_caf_file(self, conf, cod_dte):
        caf = next((l.caf for l in conf.caf_files_ids if l.name == cod_dte), False)
        return [base64.b64encode(caf.encode())] if caf else []

    def _dte_verifica_folio(self, conf, cod_dte, folio):
        folio = int(folio)
        rangos = [(int(l.desde), int(l.hasta)) for l in conf.caf_files_ids if l.name == cod_dte]
        if not rangos:
            raise UserError(_('No hay un CAF cargado para el tipo de documento %s.') % cod_dte)
        desde, hasta = rangos[0]
        if not (desde <= folio <= hasta):
            raise UserError(_(
                'El folio está fuera del rango del CAF cargado o hay un problema '
                'en la configuración del DTE.'
            ))
        return True

    def _dte_init_signature(self, init_signature):
        return init_signature == 'V'

    def _dte_data_firma_electronica(self, conf):
        """Arma los datos de firma. Prioriza el certificado `.p12` gestionado en
        `sii.firma` (`conf.firma_id`); si no está configurado, cae de vuelta a
        los campos de texto plano de `config.dte`, mantenidos por compatibilidad
        con instalaciones que aún no migran al certificado gestionado.
        """
        if conf.firma_id:
            firma = conf.firma_id
            return collections.OrderedDict(
                priv_key=firma.priv_key,
                cert=firma.cert,
                rut_firmante=firma.subject_serial_number,
                init_signature=self._dte_init_signature(conf.init_signature),
            )
        return collections.OrderedDict(
            priv_key=conf.priv_key,
            cert=conf.cert,
            rut_firmante=conf.rut_firmante,
            init_signature=self._dte_init_signature(conf.init_signature),
        )

    def _dte_data_emisor(self, conf):
        if not conf:
            return collections.OrderedDict()
        return collections.OrderedDict(
            RUTEmisor=conf.rutemisor,
            RznSoc=conf.name,
            GiroEmis=conf.giroemisor,
            Actecos=self._dte_actecos(conf),
            DirOrigen=conf.dirorigen,
            CmnaOrigen=conf.cmnaorigen,
            CiudadOrigen=conf.ciudadorigen,
            CorreoEmisor=conf.correo or conf.company_id.email,
            Modo=conf.modo,
            NroResol=conf.nroresol,
            FchResol=conf.fchresol,
            ValorIva=int(float(conf.valoriva)),
        )

    # ------------------------------------------------------------------
    # Receptor
    # ------------------------------------------------------------------
    def _dte_revisar_cliente(self, cliente):
        """Devuelve la lista de campos obligatorios que faltan en el partner receptor."""
        etiquetas = {
            'vat': 'RUT', 'name': 'razón social', 'street': 'dirección',
            'state_id': 'región', 'city': 'ciudad', 'comuna_id': 'comuna', 'giro': 'giro',
        }
        faltantes = [etiqueta for campo, etiqueta in etiquetas.items() if not getattr(cliente, campo)]
        if not (cliente.phone or cliente.email):
            faltantes.append('teléfono o correo')
        return faltantes

    def _dte_receptor(self, partner):
        faltantes = self._dte_revisar_cliente(partner)
        if faltantes:
            raise UserError(_(
                'Faltan datos del cliente "%s" para emitir el DTE: %s.'
            ) % (partner.name, ', '.join(faltantes)))
        return collections.OrderedDict(
            RUTRecep=partner.vat,
            RznSocRecep=partner.name,
            Contacto=partner.phone or partner.email,
            GiroRecep=partner.giro,
            DirRecep=partner.street,
            CmnaRecep=partner.comuna_id.name,
            CiudadRecep=partner.city,
        )

    # ------------------------------------------------------------------
    # Referencias
    # ------------------------------------------------------------------
    def _dte_referencias(self, referencias_ids, cod_dte, requiere_referencia=('61',)):
        res = []
        for i, ref in enumerate(referencias_ids, start=1):
            ref_dicc = collections.OrderedDict()
            ref_dicc['NroLinRef'] = i
            ref_dicc['TpoDocRef'] = ref.tipo_documento.codigo
            if ref.ref_global:
                ref_dicc['IndGlobal'] = 'true'
                ref_dicc['FolioRef'] = '0'
            else:
                ref_dicc['FolioRef'] = ref.folio
            ref_dicc['FchRef'] = ref.fecha_documento.strftime('%Y-%m-%d')
            ref_dicc['CodRef'] = ref.codigo_ref
            ref_dicc['RazonRef'] = ref.motivo
            res.append(ref_dicc)
        if not res and cod_dte in requiere_referencia:
            raise UserError(_('Debe agregar al menos una referencia para este tipo de documento.'))
        return res
