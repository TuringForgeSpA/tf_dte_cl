# -*- coding: utf-8 -*-
import csv
import logging
import re
import unicodedata

from odoo.modules.module import get_resource_path

_logger = logging.getLogger(__name__)

# Alias explícito para nombres de región que podrían no coincidir textualmente
# con el `name` almacenado en res.country.state (el texto exacto puede variar
# según la versión o el idioma de instalación de Odoo). Se intenta primero una
# coincidencia normalizada exacta y, si falla, una coincidencia por substring
# antes de recurrir a este mapa como último recurso.
ALIAS_REGIONES = {
    'del Ñuble': 'Ñuble',
    "del Libertador Gral. Bernardo O'Higgins": "O'Higgins",
    'de la Araucania': 'Araucanía',
    'del Maule': 'Maule',
    'del BíoBio': 'Biobío',
    'de los Lagos': 'Los Lagos',
    'Aysén del Gral. Carlos Ibáñez del Campo': 'Aysén',
}


def _normalizar(texto):
    texto = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode()
    texto = texto.replace("'", '')
    texto = re.sub(r'[^a-zA-Z0-9]+', ' ', texto)
    return texto.lower().strip()


def _resolver_region(env, chile, nombre_region, cache, candidatos):
    if nombre_region in cache:
        return cache[nombre_region]

    objetivo = _normalizar(ALIAS_REGIONES.get(nombre_region, nombre_region))

    region = candidatos.filtered(lambda s: _normalizar(s.name) == objetivo)
    if not region:
        region = candidatos.filtered(
            lambda s: objetivo in _normalizar(s.name) or _normalizar(s.name) in objetivo
        )

    cache[nombre_region] = region[:1]
    return cache[nombre_region]


def _cargar_comunas(env):
    """Puebla res.comuna con el catálogo oficial de 346 comunas de Chile.

    El emparejamiento con res.country.state se hace por nombre normalizado
    (sin tildes, insensible a mayúsculas) en lugar de por external ID, porque
    los external ID de las regiones de Chile en el módulo `base` no están
    documentados de forma estable entre versiones de Odoo, y un external ID
    incorrecto haría fallar la carga completa del archivo. Si alguna región
    no logra emparejarse, la comuna se crea igual (sin `state_id`) y queda
    registrada en el log para revisión manual — nunca bloquea la instalación.

    Idempotente: en una reinstalación o actualización, omite las comunas
    cuyo código INE ya existe.
    """
    chile = env['res.country'].search([('code', '=', 'CL')], limit=1)
    if not chile:
        _logger.warning('No se encontró el país Chile; no se cargó el catálogo de comunas.')
        return

    ruta = get_resource_path('tf_dte_cl', 'data', 'res_comuna_seed.csv')
    if not ruta:
        _logger.warning('No se encontró data/res_comuna_seed.csv; no se cargó el catálogo de comunas.')
        return

    existentes = {c.codigo for c in env['res.comuna'].search([('country_id', '=', chile.id)])}
    candidatos_region = env['res.country.state'].search([('country_id', '=', chile.id)])
    cache_regiones = {}
    sin_region = []
    vals_list = []

    with open(ruta, encoding='utf-8') as f:
        for fila in csv.DictReader(f):
            if fila['codigo'] in existentes:
                continue
            region = _resolver_region(env, chile, fila['region'], cache_regiones, candidatos_region)
            if not region:
                sin_region.append('%s (%s / %s)' % (fila['name'], fila['codigo'], fila['region']))
            vals_list.append({
                'name': fila['name'],
                'codigo': fila['codigo'],
                'country_id': chile.id,
                'state_id': region.id if region else False,
            })

    if vals_list:
        env['res.comuna'].create(vals_list)
        _logger.info('Catálogo de comunas: %s comuna(s) creada(s).', len(vals_list))

    if sin_region:
        _logger.warning(
            'Catálogo de comunas: no se pudo emparejar la región para %s comuna(s); '
            'quedaron creadas sin state_id. Revisar y completar manualmente en '
            'Ajustes > Técnico > Comunas:\n%s',
            len(sin_region), '\n'.join(sin_region),
        )
        if not candidatos_region:
            _logger.warning(
                'No se encontró ninguna res.country.state para Chile en esta base de datos. '
                'Verifique que las 16 regiones estén cargadas (vienen incluidas en el módulo '
                '"base" desde Odoo 13) antes de reintentar.'
            )
