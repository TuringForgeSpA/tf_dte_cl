{
    'name': 'Facturación Electrónica Chile (SII)',
    'version': '18.0.1.0.0',
    'category': 'Accounting/Localizations/EDI',
    'summary': 'Generación y envío de DTE al SII para Chile mediante framework EDI.',
    'author': 'Turing Forge SpA',
    'website': 'https://www.turingforge.cl',
    'license': 'AGPL-3',
    'depends': [
        'account',
        'account_edi',
        'l10n_cl', # Dependencia para usar los datos oficiales
    ],
    'external_dependencies': {
        'python': ['facturacion_electronica'], # Aquí se declara la librería de PIP de facturación electrónica
    },
    'data': [
        'security/ir.model.access.csv',
        'security/state_manager.xml',
        'data/account_edi_data.xml',
        'data/account.move.docs.sii.csv',
        'data/comunas_utf.csv',
        # ... el resto de las vistas ...
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
