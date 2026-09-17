# -*- coding: utf-8 -*-
"""Hooks de instalación de tf_dte_cl.

Ruta real: hooks.py
"""
import csv
import logging
import re
import unicodedata

from odoo.tools.misc import file_open

_logger = logging.getLogger(__name__)

SEED_PATH = 'tf_dte_cl/data/res_comuna_seed.csv'

# Alias para nombres de región del CSV que no coinciden textualmente con
# res.country.state (el texto exacto depende de la versión y el idioma de Odoo).
REGION_ALIASES = {
    'del Ñuble': 'Ñuble',
    "del Libertador Gral. Bernardo O'Higgins": "O'Higgins",
    'de la Araucania': 'Araucanía',
    'del Maule': 'Maule',
    'del BíoBio': 'Biobío',
    'de los Lagos': 'Los Lagos',
    'Aysén del Gral. Carlos Ibáñez del Campo': 'Aysén',
}


def _normalize(text):
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode()
    text = re.sub(r'[^a-zA-Z0-9]+', ' ', text.replace("'", ''))
    return text.lower().strip()


def _resolve_region(region_name, states, cache):
    if region_name not in cache:
        target = _normalize(REGION_ALIASES.get(region_name, region_name))
        state = states.filtered(lambda s: _normalize(s.name) == target)
        if not state:
            state = states.filtered(
                lambda s: target in _normalize(s.name) or _normalize(s.name) in target
            )
        cache[region_name] = state[:1]
    return cache[region_name]


def _tf_dte_cl_load_comunas(env):
    """Carga el catálogo de comunas. Idempotente: omite los códigos existentes.

    La región se empareja por nombre normalizado; si no se encuentra, la comuna
    se crea sin región y queda registrada en el log, sin bloquear la instalación.
    """
    chile = env.ref('base.cl', raise_if_not_found=False)
    if not chile:
        _logger.warning('No se encontró el país Chile; no se cargó el catálogo de comunas.')
        return
    Comuna = env['tf_dte_cl.comuna'].with_context(active_test=False)
    existing = set(Comuna.search([('country_id', '=', chile.id)]).mapped('code'))
    states = env['res.country.state'].search([('country_id', '=', chile.id)])
    cache, without_region, vals_list = {}, [], []

    # file_open abre en UTF-8 cuando el modo es texto; no acepta el argumento encoding.
    with file_open(SEED_PATH, mode='r') as seed:
        for row in csv.DictReader(seed):
            code = str(row['codigo']).strip()
            if code in existing:
                continue
            state = _resolve_region(row['region'], states, cache)
            if not state:
                without_region.append('%s (%s / %s)' % (row['name'], code, row['region']))
            vals_list.append({
                'name': row['name'].strip(),
                'code': code,
                'country_id': chile.id,
                'state_id': state.id,
            })
            existing.add(code)

    if vals_list:
        Comuna.create(vals_list)
        _logger.info('Catálogo de comunas: %s comuna(s) creada(s).', len(vals_list))
    if without_region:
        _logger.warning(
            'Catálogo de comunas: %s comuna(s) quedaron sin región; complételas en '
            'Facturación electrónica > Configuración > Comunas:\n%s',
            len(without_region), '\n'.join(without_region),
        )
